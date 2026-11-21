"""C1 - match ingest. See PLAN.md section 3.

Pulls your match list, fetches full detail for anything new, and persists the
roster, every kill with its millisecond offset, and your highlights.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import timezone

from clipsvalorant import db
from clipsvalorant.cancel import CancelToken, NullToken
from clipsvalorant.config import Config
from clipsvalorant.henrik import BULK_SIZE, HenrikClient, HenrikError
from clipsvalorant.kills import find_highlights, is_player
from clipsvalorant.models import Match

log = logging.getLogger(__name__)

# The listing endpoint answers one page at a time. One hundred is the largest
# page this project has seen it return, and a thirty-day window for an active
# player is comfortably more than that, so a window wider than one page has to
# be walked rather than asked for in a single call.
PAGE_SIZE = 100
# Purely a guard against a `page` parameter that silently does nothing. The
# real stop conditions are the window, the end of the history, and `limit`.
MAX_PAGES = 12


@dataclass
class IngestReport:
    seen: int = 0
    stored: int = 0
    skipped: int = 0
    failed: int = 0
    highlights: int = 0
    too_old: int = 0
    pages: int = 0
    # True when the listing was walked all the way out of the window, or the
    # history ran out before the window did. False means `limit` stopped it
    # first and there are older matches inside the window nobody looked at.
    # The caller is expected to say so rather than present a partial answer
    # as a whole one.
    complete: bool = True
    # The oldest match the listing kept. It is how deep the window was
    # actually covered only when `complete` is False, because a walk that
    # ran to the cutoff deliberately keeps the whole page it crossed on -
    # entries past the window and all, which `too_old` then drops.
    oldest_listed: int | None = None
    # How many of the new matches arrived in the one bulk answer rather than
    # costing a call each. Reported because the whole point of the bulk call
    # is quota, and a number nobody can see is a claim nobody can check.
    in_bulk: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{self.seen} match(es) listed",
            f"{self.stored} new",
            f"{self.skipped} already known",
            f"{self.highlights} highlight(s)",
        ]
        if self.in_bulk:
            parts.append(f"{self.in_bulk} of them in one call")
        if self.pages > 1:
            parts.append(f"{self.pages} listing pages")
        if self.too_old:
            parts.append(f"{self.too_old} past the window, not fetched")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if not self.complete:
            parts.append("stopped at the ceiling, older matches not listed")
        return ", ".join(parts)


def _epoch(dt) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _listed_epoch(entry) -> int | None:
    """When the listing says this match started, or None if it does not say.

    A field that moves or vanishes must not silently empty a whole scan, so
    'no date' means 'fetch it and see', never 'too old'.
    """
    try:
        return _epoch(entry.meta.started_at)
    except (AttributeError, TypeError, ValueError):
        return None


def _bulk_head(
    client,
    cfg: Config,
    listed: list,
    already: set[str],
    *,
    mode: str | None,
    refetch: bool,
    report: IngestReport,
) -> dict[str, Match]:
    """Buy the newest unknown matches in one call instead of one call each.

    `/v4/matches` returns the ten most recent matches in full for a single
    point, where ten `match_detail` calls cost eleven. It only reaches the
    ten most recent, and it filters by mode exactly as the listing does, so
    the matches it can answer for are the head of the walk we just did.

    Worth one point only when it covers at least two of them: for a single
    new match it is the same one call either way, and a scan with nothing
    new should spend nothing at all. A failure here is not an error - every
    match it would have covered simply costs its own call, as before.
    """
    head = listed[:BULK_SIZE]
    need = {e.meta.id for e in head if refetch or e.meta.id not in already}
    if len(need) < 2:
        return {}

    try:
        batch = client.recent_matches(
            cfg.riot_name, cfg.riot_tag, cfg.region, mode=mode, size=BULK_SIZE
        )
    except HenrikError as exc:
        log.info("The bulk answer did not come; falling back to one call each (%s)", exc)
        return {}

    out = {m.metadata.match_id: m for m in batch if m.metadata.match_id in need}
    report.in_bulk = len(out)
    return out


def _list_window(
    client,
    cfg: Config,
    *,
    mode: str | None,
    limit: int,
    since_epoch: int | None,
    report: IngestReport,
    cancel: CancelToken,
) -> list:
    """Page through the listing until it leaves the window, or runs out.

    Asking for `size=100` returns the hundred most recent matches and nothing
    else, so a scan that never asked for page two could only ever cover as
    many days as a hundred matches happen to span. For an active player that
    is three weeks, which is how choosing thirty days quietly returned 69% of
    them. The window decides the depth now; `limit` is the ceiling that keeps
    it from becoming unbounded, and when the ceiling is what stopped the walk
    `report.complete` is False so the caller can say so.
    """
    out: list = []
    seen_ids: set[str] = set()
    # Fixed for the whole walk, deliberately. Which matches page two holds
    # depends on the size page one was asked with, so shrinking the size to
    # fit the remaining ceiling would ask for a page that overlaps the one
    # already read - and the overlap looks exactly like the end of the
    # history. Over-reading the last page costs nothing: it is the same one
    # call either way, and anything past the ceiling is trimmed below.
    size = min(PAGE_SIZE, max(1, limit))
    page = 1
    complete = True

    while True:
        cancel.check()
        chunk = client.stored_matches(
            cfg.riot_name,
            cfg.riot_tag,
            cfg.region,
            mode=mode,
            size=size,
            page=page,
        )
        report.pages = page

        fresh = [e for e in chunk if e.meta.id not in seen_ids]
        if not fresh:
            if len(chunk) >= size:
                # A full page of matches already read means `page` was
                # accepted and ignored, not that the history ended. Calling
                # that complete would be the exact lie this walk exists to
                # stop telling.
                complete = False
            break
        for entry in fresh:
            seen_ids.add(entry.meta.id)
        out.extend(fresh)

        # The window first, then the end of the history, then the ceiling.
        # A page that both crosses the cutoff and fills the ceiling covered
        # the window, and saying otherwise would cry wolf.
        if since_epoch is not None:
            dated = [w for w in (_listed_epoch(e) for e in fresh) if w is not None]
            if dated and min(dated) < since_epoch:
                break
        if len(fresh) < size:
            break
        if len(out) >= limit:
            complete = False
            break
        page += 1
        if page > MAX_PAGES:
            complete = False
            break

    kept = out[:limit]
    dropped = out[limit:]
    if dropped and (
        since_epoch is None
        or any(
            w is not None and w >= since_epoch
            for w in (_listed_epoch(e) for e in dropped)
        )
    ):
        # Trimming threw away a match that was inside the window, which is
        # the one case the caller must not present as a complete answer.
        complete = False

    report.complete = complete
    dates = [w for w in (_listed_epoch(e) for e in kept) if w is not None]
    report.oldest_listed = min(dates) if dates else None
    return kept


def persist_match(
    conn: sqlite3.Connection,
    match: Match,
    *,
    my_name: str,
    my_tag: str,
    region: str,
    raw: dict | None = None,
) -> int:
    """Write one match and everything derived from it. Returns highlights found.

    Raises LookupError if the configured Riot ID is not in this match's roster,
    which almost always means RIOT_NAME / RIOT_TAG are wrong.
    """
    me = match.find_me(my_name, my_tag)
    if me is None:
        roster = ", ".join(f"{p.name}#{p.tag}" for p in match.players)
        raise LookupError(
            f"{my_name}#{my_tag} is not in match {match.id}. Roster was: {roster}"
        )

    meta = match.metadata
    started = _epoch(meta.started_at)
    length_ms = meta.game_length_in_ms
    ended = started + (length_ms // 1000) if length_ms else None

    my_team_row = next((t for t in match.teams if t.team_id == me.team), None)
    rounds_won = my_team_row.rounds.won if my_team_row else None
    rounds_lost = my_team_row.rounds.lost if my_team_row else None
    my_team_won = my_team_row.won if my_team_row else None

    conn.execute(
        """
        INSERT INTO match (id, started_at, started_at_epoch, game_length_ms, ended_at_epoch,
                           map, map_id, mode, queue, region, cluster, my_team,
                           my_team_won, rounds_won, rounds_lost, ingested_at, raw_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            ingested_at = excluded.ingested_at,
            raw_json    = excluded.raw_json,
            map_id      = excluded.map_id
        """,
        (
            match.id,
            meta.started_at.isoformat(),
            started,
            length_ms,
            ended,
            meta.map_name,
            meta.map_id,
            meta.mode,
            meta.queue,
            meta.region or region,
            meta.cluster,
            me.team,
            None if my_team_won is None else int(my_team_won),
            rounds_won,
            rounds_lost,
            db.utcnow(),
            json.dumps(raw if raw is not None else match.model_dump(mode="json")),
        ),
    )

    # Roster. Every one of these 10 is a future lookup candidate in Phase 4.
    for player in match.players:
        mine = player.puuid == me.puuid
        conn.execute(
            """
            INSERT INTO match_player (match_id, puuid, name, tag, team, agent,
                                      agent_id, tier, kills, deaths, assists, score,
                                      headshots, bodyshots, legshots,
                                      damage_dealt, damage_received, is_me)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(match_id, puuid) DO UPDATE SET
                name = excluded.name, tag = excluded.tag, team = excluded.team,
                agent = excluded.agent, agent_id = excluded.agent_id,
                tier = excluded.tier, kills = excluded.kills,
                deaths = excluded.deaths, assists = excluded.assists,
                score = excluded.score, headshots = excluded.headshots,
                bodyshots = excluded.bodyshots, legshots = excluded.legshots,
                damage_dealt = excluded.damage_dealt,
                damage_received = excluded.damage_received
            """,
            (
                match.id,
                player.puuid,
                player.name,
                player.tag,
                player.team,
                player.agent,
                player.agent_id,
                player.tier,
                player.stats.kills,
                player.stats.deaths,
                player.stats.assists,
                player.stats.score,
                player.stats.headshots,
                player.stats.bodyshots,
                player.stats.legshots,
                player.stats.damage.dealt,
                player.stats.damage.received,
                int(mine),
            ),
        )
        db.upsert_account(
            conn,
            puuid=player.puuid,
            name=player.name,
            tag=player.tag,
            region=meta.region or region,
            is_me=mine,
        )

    # Rounds, for the timeline. Everything here is also in raw_json; it is
    # written out so the page does not have to re-parse 400KB to find out who
    # won round seven.
    for entry in match.rounds:
        if entry.id is None:
            continue
        plant = entry.plant
        conn.execute(
            """
            INSERT INTO round (match_id, round, result, winning_team, winning_role,
                               ceremony, plant_ms, plant_site, plant_x, plant_y,
                               defuse_ms)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(match_id, round) DO UPDATE SET
                result = excluded.result, winning_team = excluded.winning_team,
                winning_role = excluded.winning_role, ceremony = excluded.ceremony,
                plant_ms = excluded.plant_ms, plant_site = excluded.plant_site,
                plant_x = excluded.plant_x, plant_y = excluded.plant_y,
                defuse_ms = excluded.defuse_ms
            """,
            (
                match.id,
                entry.id,
                entry.result,
                entry.winning_team,
                entry.winning_team_role,
                entry.ceremony,
                plant.round_time_in_ms if plant else None,
                plant.site if plant else None,
                plant.location.x if plant else None,
                plant.location.y if plant else None,
                entry.defuse.round_time_in_ms if entry.defuse else None,
            ),
        )

    # Kills. time_in_match_ms is the number every VOD timestamp is derived from.
    for kill in match.kills:
        conn.execute(
            """
            INSERT OR IGNORE INTO kill (match_id, round, time_in_match_ms, time_in_round_ms,
                                        killer_puuid, killer_name, victim_puuid, victim_name,
                                        weapon, assist_count, is_mine, x, y)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                match.id,
                kill.round,
                kill.time_in_match_in_ms,
                kill.time_in_round_in_ms,
                kill.killer.puuid,
                kill.killer.riot_id,
                kill.victim.puuid,
                kill.victim.riot_id,
                kill.weapon,
                len(kill.assistants),
                int(is_player(kill.killer, me.puuid, me.name, me.tag)),
                kill.location.x,
                kill.location.y,
            ),
        )
        # A row that predates coordinates is updated in place rather than
        # ignored, so a backfill actually lands on the rows already there.
        if kill.location.known:
            conn.execute(
                """
                UPDATE kill SET x = ?, y = ?
                 WHERE match_id = ? AND round = ? AND time_in_match_ms = ?
                   AND x IS NULL
                """,
                (kill.location.x, kill.location.y, match.id, kill.round,
                 kill.time_in_match_in_ms),
            )

    highlights = find_highlights(match, me.puuid)
    for h in highlights:
        conn.execute(
            """
            INSERT INTO highlight (match_id, round, kind, label, kill_count,
                                   time_in_match_ms, end_time_in_match_ms, round_won,
                                   ceremony, distinct_victims)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(match_id, round, kind) DO UPDATE SET
                label = excluded.label, kill_count = excluded.kill_count,
                time_in_match_ms = excluded.time_in_match_ms,
                end_time_in_match_ms = excluded.end_time_in_match_ms,
                round_won = excluded.round_won,
                ceremony = excluded.ceremony,
                distinct_victims = excluded.distinct_victims
            """,
            (
                match.id,
                h.round,
                h.kind,
                h.label,
                h.kill_count,
                h.time_in_match_ms,
                h.end_time_in_match_ms,
                None if h.round_won is None else int(h.round_won),
                h.ceremony,
                h.distinct_victims,
            ),
        )

    return len(highlights)


def ingest(
    cfg: Config,
    *,
    limit: int = 20,
    mode: str | None = "competitive",
    refetch: bool = False,
    since_epoch: int | None = None,
    cancel: CancelToken | None = None,
) -> IngestReport:
    """Store every match inside the window, up to `limit` of them.

    `since_epoch` is the oldest match worth having, and it drives how deep
    the listing is walked: the endpoint pages, and a scan that only ever read
    page one covered as many days as a hundred matches happened to span.
    `limit` is the ceiling that stops a wide window becoming unbounded API
    calls, and when it is what ended the walk the report says so.

    The listing already carries each match's start time, so a match older
    than the window can be dropped before its detail is fetched - and the
    detail is the expensive part. HenrikDev charges a point per call plus one
    per Riot request it triggers behind the scenes, and when the window runs
    dry the client sleeps it out, so fetching forty matches to resolve twelve
    is not merely wasteful, it is most of the wall clock.
    """
    report = IngestReport()
    cancel = cancel or NullToken()

    with db.session(cfg.db_path) as conn:
        already = db.known_match_ids(conn)

        with HenrikClient(cfg.henrik_api_key, cancel=cancel) as client:
            listed = _list_window(
                client,
                cfg,
                mode=mode,
                limit=limit,
                since_epoch=since_epoch,
                report=report,
                cancel=cancel,
            )
            report.seen = len(listed)
            in_bulk = _bulk_head(client, cfg, listed, already, mode=mode,
                                 refetch=refetch, report=report)

            for entry in listed:
                # Between matches, not inside one: a half-written match is
                # worse than one more fetch.
                cancel.check()
                match_id = entry.meta.id
                if match_id in already and not refetch:
                    report.skipped += 1
                    continue
                if since_epoch is not None:
                    when = _listed_epoch(entry)
                    if when is not None and when < since_epoch:
                        report.too_old += 1
                        continue

                try:
                    match = in_bulk.get(match_id) or client.match_detail(
                        match_id, cfg.region
                    )
                    found = persist_match(
                        conn,
                        match,
                        my_name=cfg.riot_name,
                        my_tag=cfg.riot_tag,
                        region=cfg.region,
                        raw=match.raw or None,
                    )
                except (HenrikError, LookupError, ValueError) as exc:
                    report.failed += 1
                    report.errors.append(f"{match_id}: {exc}")
                    log.warning("Failed on %s: %s", match_id, exc)
                    continue

                conn.commit()
                report.stored += 1
                report.highlights += found

    return report


def other_modes(
    cfg: Config,
    *,
    mode: str | None,
    since_epoch: int | None,
    limit: int = 100,
    cancel: CancelToken | None = None,
) -> Counter:
    """The queues this scan is not looking at, and how many matches of each.

    One listing call, and it buys the difference between "45 matches" and
    "45 of 69". A scan filtered to Competitive can be two thirds of a busy
    account's fortnight, with the rest in Swiftplay, Deathmatch and custom
    games - and nothing used to say so, which makes a match played in the
    wrong queue indistinguishable from a scanner that is broken.
    """
    found: Counter = Counter()
    want = (mode or "").strip().lower()
    if not want:
        return found  # nothing is being filtered out, so nothing is hidden

    cancel = cancel or NullToken()
    with HenrikClient(cfg.henrik_api_key, cancel=cancel) as client:
        listed = _list_window(
            client,
            cfg,
            mode=None,
            limit=limit,
            since_epoch=since_epoch,
            report=IngestReport(),
            cancel=cancel,
        )

    for entry in listed:
        if since_epoch is not None:
            when = _listed_epoch(entry)
            if when is not None and when < since_epoch:
                continue
        label = (entry.meta.mode or "Unknown").strip()
        if label.lower() != want:
            found[label] += 1
    return found


def needs_backfill(conn) -> list[str]:
    """Matches stored before the payload was kept whole, newest first.

    Identified by the map's uuid being absent, because that arrives in the
    same payload as the kill coordinates and the per-player damage: if one
    is missing they all are.
    """
    rows = conn.execute(
        "SELECT id FROM match WHERE map_id IS NULL ORDER BY started_at_epoch DESC"
    ).fetchall()
    return [r["id"] for r in rows]


def backfill(cfg: Config, *, limit: int = 25) -> IngestReport:
    """Re-fetch matches whose stored payload was trimmed on the way in.

    This costs real API calls - one per match, plus whatever Riot requests
    HenrikDev makes behind each one - which is why it is a command of its own
    and not something a scan does quietly. For a long while `raw_json` held
    this model's own dump rather than the payload, and the model is declared
    to ignore fields it does not know, so kill coordinates, per-player damage
    and the map's uuid never reached the disk at all. Nothing can recover
    them locally; they have to be asked for again.

    Newest first, because Twitch's retention means a two-week-old match is
    the oldest one whose broadcast could still exist to put the map beside.
    """
    report = IngestReport()
    with db.session(cfg.db_path) as conn:
        pending = needs_backfill(conn)
        report.seen = len(pending)
        if not pending:
            return report
        with HenrikClient(cfg.henrik_api_key) as client:
            for match_id in pending[:limit]:
                try:
                    match = client.match_detail(match_id, cfg.region)
                    found = persist_match(
                        conn,
                        match,
                        my_name=cfg.riot_name,
                        my_tag=cfg.riot_tag,
                        region=cfg.region,
                        raw=match.raw or None,
                    )
                except (HenrikError, LookupError, ValueError) as exc:
                    report.failed += 1
                    report.errors.append(f"{match_id}: {exc}")
                    continue
                report.stored += 1
                report.highlights += found
                conn.commit()
        report.skipped = max(0, len(pending) - limit)
    return report


def recompute(cfg: Config) -> IngestReport:
    """Re-derive kills and highlights from stored raw JSON. Costs no API calls.

    raw_json is kept precisely so detection logic can change without re-fetching.
    """
    report = IngestReport()
    with db.session(cfg.db_path) as conn:
        rows = conn.execute("SELECT id, raw_json FROM match").fetchall()
        report.seen = len(rows)
        for row in rows:
            try:
                match = Match.model_validate(json.loads(row["raw_json"]))
            except (ValueError, TypeError) as exc:
                report.failed += 1
                report.errors.append(f"{row['id']}: unparseable raw_json: {exc}")
                continue
            # Highlight definitions may have changed shape, so clear and rebuild.
            conn.execute("DELETE FROM highlight WHERE match_id = ?", (row["id"],))
            conn.execute("DELETE FROM kill WHERE match_id = ?", (row["id"],))
            try:
                found = persist_match(
                    conn,
                    match,
                    my_name=cfg.riot_name,
                    my_tag=cfg.riot_tag,
                    region=cfg.region,
                    raw=json.loads(row["raw_json"]),
                )
            except LookupError as exc:
                report.failed += 1
                report.errors.append(f"{row['id']}: {exc}")
                continue
            report.stored += 1
            report.highlights += found
        conn.commit()
    return report


def ingest_file(cfg: Config, path: str) -> IngestReport:
    """Load a saved v4 match JSON from disk. Works without an API key."""
    report = IngestReport(seen=1)
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)

    data = payload.get("data", payload)
    match = Match.model_validate(data)

    with db.session(cfg.db_path) as conn:
        found = persist_match(
            conn,
            match,
            my_name=cfg.riot_name,
            my_tag=cfg.riot_tag,
            region=cfg.region,
            raw=data,
        )
    report.stored = 1
    report.highlights = found
    return report
