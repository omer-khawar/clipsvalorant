"""C1 - match ingest. See PLAN.md section 3.

Pulls your match list, fetches full detail for anything new, and persists the
roster, every kill with its millisecond offset, and your highlights.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import timezone

from clipsvalorant import db
from clipsvalorant.config import Config
from clipsvalorant.henrik import HenrikClient, HenrikError
from clipsvalorant.kills import find_highlights, is_player
from clipsvalorant.models import Match

log = logging.getLogger(__name__)


@dataclass
class IngestReport:
    seen: int = 0
    stored: int = 0
    skipped: int = 0
    failed: int = 0
    highlights: int = 0
    too_old: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{self.seen} match(es) listed",
            f"{self.stored} new",
            f"{self.skipped} already known",
            f"{self.highlights} highlight(s)",
        ]
        if self.too_old:
            parts.append(f"{self.too_old} past the window, not fetched")
        if self.failed:
            parts.append(f"{self.failed} failed")
        return ", ".join(parts)


def _epoch(dt) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


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
                           map, mode, queue, region, cluster, my_team, my_team_won,
                           rounds_won, rounds_lost, ingested_at, raw_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            ingested_at = excluded.ingested_at,
            raw_json    = excluded.raw_json
        """,
        (
            match.id,
            meta.started_at.isoformat(),
            started,
            length_ms,
            ended,
            meta.map_name,
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
                                      kills, deaths, assists, score, is_me)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(match_id, puuid) DO UPDATE SET
                name = excluded.name, tag = excluded.tag, team = excluded.team,
                agent = excluded.agent, kills = excluded.kills,
                deaths = excluded.deaths, assists = excluded.assists,
                score = excluded.score
            """,
            (
                match.id,
                player.puuid,
                player.name,
                player.tag,
                player.team,
                player.agent,
                player.stats.kills,
                player.stats.deaths,
                player.stats.assists,
                player.stats.score,
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

    # Kills. time_in_match_ms is the number every VOD timestamp is derived from.
    for kill in match.kills:
        conn.execute(
            """
            INSERT OR IGNORE INTO kill (match_id, round, time_in_match_ms, time_in_round_ms,
                                        killer_puuid, killer_name, victim_puuid, victim_name,
                                        weapon, assist_count, is_mine)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
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
            ),
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
) -> IngestReport:
    """Fetch the most recent `limit` matches and store any we do not already have.

    `since_epoch` is the oldest match worth having. The listing already
    carries each match's start time, so a match older than that can be
    dropped before its detail is fetched - and the detail is the expensive
    part. HenrikDev charges a point per call plus one per Riot request it
    triggers behind the scenes, and when the window runs dry the client
    sleeps it out, so fetching forty matches to resolve twelve is not merely
    wasteful, it is most of the wall clock.
    """
    report = IngestReport()

    with db.session(cfg.db_path) as conn:
        already = db.known_match_ids(conn)

        with HenrikClient(cfg.henrik_api_key) as client:
            listed = client.stored_matches(
                cfg.riot_name, cfg.riot_tag, cfg.region, mode=mode, size=limit
            )
            report.seen = len(listed)

            for entry in listed:
                match_id = entry.meta.id
                if match_id in already and not refetch:
                    report.skipped += 1
                    continue
                if since_epoch is not None:
                    try:
                        when = _epoch(entry.meta.started_at)
                    except (AttributeError, TypeError, ValueError):
                        when = None  # no date on the listing: fetch it and see
                    if when is not None and when < since_epoch:
                        report.too_old += 1
                        continue

                try:
                    match = client.match_detail(match_id, cfg.region)
                    found = persist_match(
                        conn,
                        match,
                        my_name=cfg.riot_name,
                        my_tag=cfg.riot_tag,
                        region=cfg.region,
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
