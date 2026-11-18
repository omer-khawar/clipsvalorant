"""Phase 2 - work out which players in a match were streaming, and where to look.

The funnel, cheapest first (PLAN.md section 0):

  Layer 2  alias table         one local query, free
  Layer 1  name match          ~1 Twitch call per player
  Layer 3  Riot ID harvest     bio + VOD titles, regex for NAME#TAG
  Layer 4  clip window         every Valorant clip made during the match

and then, over every candidate any layer produced, the part that actually decides:

  evidence  a VOD that covers the match, AND proof that the VOD contained
            Valorant during the match's own minutes.

That second clause is load-bearing. A VOD that merely spans the match proves only
that the channel was broadcasting something - which is true of any channel that
streams daily, and is how a lobby-mate called Curry got matched to twitch.tv/curry,
a radiant Valorant streamer who was, during that window, broadcasting Deadlock.
A name can collide; a broadcast clock cannot tell you what was on screen.

So the only thing allowed to say "confirmed" is a tie to *this* match: a clip cut
from the covering VOD, filed under Valorant, at an offset inside the match. Clips
carry video_id, vod_offset and game_id, which makes a past broadcast's game
timeline reconstructable after the fact - the one thing GET /helix/videos cannot
tell you. Everything weaker is reported as a candidate and never written to the
alias table, because a wrong alias is permanent: Layer 2 would hand it back first,
forever, and the mistake would look like history repeating as corroboration.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from clipsvalorant import db
from clipsvalorant.config import Config
from clipsvalorant.models import Match, Player
from clipsvalorant.twitch import TwitchClient, Vod

log = logging.getLogger(__name__)

# A stream may be picked up moments into agent select, so allow a little slack at
# the edges when asking whether a broadcast spans a match outright.
VOD_SLACK_S = 45

# A broadcast that holds most of the match is worth looking at even if it missed
# the start - the later rounds are still in it, and the link maths already refuses
# to point at a moment before the stream began. Curry's VOD began 129s after the
# match did; what ruled it out was having no Valorant in it, not the late start.
MIN_OVERLAP = 0.6

# How long after a broadcast people are still cutting clips out of its VOD.
CLIP_LOOKAHEAD_DAYS = 14

_NON_ALNUM = re.compile(r"[^a-z0-9]")

# A Riot ID is a 3-16 character name, then #, then a 3-5 character tag. The tag
# length rule is what keeps "FINDING #1 HUSH" and "clip #42" out of the results.
# Names may contain spaces, so the text before a # cannot be split from prose with
# any certainty - keep every plausible tail and let the comparison decide.
_RIOT_ID = re.compile(r"([A-Za-z0-9][A-Za-z0-9 _.\-]{1,30})#([A-Za-z0-9]{3,5})(?!\w)")


def normalise(name: str) -> str:
    return _NON_ALNUM.sub("", name.casefold())


def riot_ids_in(text: str | None) -> set[str]:
    """Pull NAME#TAG out of a bio or stream title. Layer 3."""
    out: set[str] = set()
    for hit in _RIOT_ID.finditer(text or ""):
        blob, tag = hit.group(1), hit.group(2)
        words = blob.split()
        tails = [" ".join(words[i:]) for i in range(len(words))]
        plausible = [t for t in tails if 3 <= len(t) <= 16]
        if not plausible and blob:
            plausible = [blob.strip()[-16:]]
        out.update(f"{t}#{tag}" for t in plausible)
    return out


def _same_id(advertised: str, player: Player) -> bool:
    """`advertised` only confirms the player if the tag AND the name agree."""
    adv_name, _, adv_tag = advertised.rpartition("#")
    return normalise(adv_tag) == normalise(player.tag) and normalise(adv_name) == normalise(
        player.name
    )


def _tags_in(advertised: tuple[str, ...]) -> set[str]:
    return {normalise(a.rpartition("#")[2]) for a in advertised}


@dataclass
class Evidence:
    """Everything we know about one (player, channel) pair, and nothing we assume."""

    name_exact: bool = False
    name_prefix: bool = False
    vod_covers: bool = False
    vod_overlap_s: int = 0
    match_len_s: int = 0
    vod_late_start_s: int = 0
    vod_early_end_s: int = 0
    vod_title: str = ""
    clips_on_vod: int = 0
    valorant_clips_on_vod: int = 0
    clips_inside_match: int = 0
    map_named_in_clip: bool = False
    advertised_ids: tuple[str, ...] = ()
    id_confirms: bool = False
    id_conflicts: bool = False

    @property
    def wrong_game(self) -> bool:
        """Clips exist for this broadcast and none of them are Valorant."""
        return self.clips_on_vod > 0 and self.valorant_clips_on_vod == 0

    @property
    def ties_to_this_match(self) -> bool:
        return self.clips_inside_match > 0 or self.id_confirms

    @property
    def vod_usable(self) -> bool:
        """Does a broadcast hold enough of the match to be worth a link?"""
        if self.vod_covers:
            return True
        return bool(self.match_len_s) and self.vod_overlap_s >= MIN_OVERLAP * self.match_len_s

    def confirmed_by(self) -> str | None:
        """Which signal, if any, ties this channel to this particular match."""
        if self.clips_inside_match > 0:
            return "clips cut from that VOD inside the match"
        if self.id_confirms:
            return "the Riot ID the channel advertises"
        return None

    def verdict(self) -> str:
        if not self.vod_usable:
            return "rejected"
        if self.clips_inside_match > 0:
            return "confirmed"
        if self.wrong_game:
            return "rejected"
        if self.id_confirms:
            return "confirmed"
        if self.id_conflicts:
            # The channel advertises a Riot ID, and it is somebody else's.
            return "rejected"
        if self.name_exact and self.valorant_clips_on_vod > 0:
            return "likely"
        return "unconfirmed"

    def reasons(self) -> list[str]:
        out: list[str] = []
        if self.name_exact:
            out.append("Twitch name matches the Riot ID exactly")
        elif self.name_prefix:
            out.append("Twitch name is a prefix-relative of the Riot ID")
        if self.vod_title:
            out.append(f'the broadcast is titled "{self.vod_title}"')
        if self.vod_covers:
            out.append("a VOD covers the whole match")
        elif self.vod_usable:
            out.append(
                f"a VOD holds {self.vod_overlap_s}s of the {self.match_len_s}s match"
            )
        else:
            out.append("no VOD holds enough of the match")
        if self.vod_late_start_s > 0:
            out.append(f"that stream began {self.vod_late_start_s}s after the match did")
        if self.vod_early_end_s > 0:
            out.append(f"that stream ended {self.vod_early_end_s}s before the match did")
        if self.clips_inside_match:
            out.append(
                f"{self.clips_inside_match} Valorant clip(s) cut from that VOD "
                "at offsets inside the match"
            )
        elif self.valorant_clips_on_vod:
            out.append(
                f"{self.valorant_clips_on_vod} Valorant clip(s) from that VOD, "
                "none during the match"
            )
        elif self.wrong_game:
            out.append(
                f"{self.clips_on_vod} clip(s) from that VOD and not one is Valorant - "
                "they were streaming something else"
            )
        else:
            out.append("nobody clipped that VOD, so its game is unknown")
        if self.map_named_in_clip:
            out.append("a clip title names this match's map")
        if self.id_confirms:
            out.append("the channel advertises this exact Riot ID")
        elif self.id_conflicts:
            out.append(f"the channel advertises a different Riot ID ({', '.join(self.advertised_ids)})")
        return out


@dataclass
class Candidate:
    match_id: str
    player: Player
    channel_id: str
    channel_login: str
    display_name: str
    source: str
    vod: Vod | None = None
    evidence: Evidence = field(default_factory=Evidence)

    @property
    def verdict(self) -> str:
        return self.evidence.verdict()

    @property
    def confidence(self) -> float:
        return {"confirmed": 0.95, "likely": 0.5, "unconfirmed": 0.25, "rejected": 0.0}[
            self.verdict
        ]


@dataclass
class ResolveReport:
    matches_checked: int = 0
    players_checked: int = 0
    name_candidates: int = 0
    clips_seen: int = 0
    clip_channels: int = 0
    bios_read: int = 0
    confirmed: list[Candidate] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    rejected: list[Candidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def file(self, cand: Candidate) -> None:
        {"confirmed": self.confirmed, "likely": self.candidates,
         "unconfirmed": self.candidates, "rejected": self.rejected}[cand.verdict].append(cand)

    def summary(self) -> str:
        return (
            f"{self.matches_checked} match(es), {self.players_checked} player(s) checked, "
            f"{self.name_candidates} name candidate(s), "
            f"{self.clips_seen} clip(s) from {self.clip_channels} channel(s), "
            f"{self.bios_read} bio(s) read, "
            f"{len(self.confirmed)} confirmed, {len(self.candidates)} unverified, "
            f"{len(self.rejected)} ruled out"
        )


def vod_link(vod_id: str, offset_s: int) -> str:
    """A VOD deep link. Twitch wants h/m/s, and negative offsets are meaningless."""
    offset_s = max(offset_s, 0)
    hours, rest = divmod(offset_s, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"https://www.twitch.tv/videos/{vod_id}?t={hours:02d}h{minutes:02d}m{seconds:02d}s"


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _known_aliases(conn: sqlite3.Connection, name: str, tag: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.channel_id, a.source, a.confidence, c.login, c.display_name
        FROM alias a LEFT JOIN twitch_channel c ON c.id = a.channel_id
        WHERE a.riot_name = ? COLLATE NOCASE AND a.riot_tag = ? COLLATE NOCASE
        """,
        (name, tag),
    ).fetchall()


def _remember_channel(conn: sqlite3.Connection, row: dict) -> None:
    now = db.utcnow()
    conn.execute(
        """
        INSERT INTO twitch_channel (id, login, display_name, description, first_seen, last_seen)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            login = excluded.login,
            display_name = excluded.display_name,
            description = COALESCE(excluded.description, twitch_channel.description),
            last_seen = excluded.last_seen
        """,
        (
            row["id"],
            row.get("broadcaster_login") or row.get("login") or "",
            row.get("display_name"),
            row.get("description"),
            now,
            now,
        ),
    )


def _remember_vod(conn: sqlite3.Connection, vod: Vod) -> None:
    conn.execute(
        """
        INSERT INTO twitch_vod (id, channel_id, stream_id, title, created_at,
                                created_epoch, duration_s, end_epoch, url, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            duration_s = excluded.duration_s, end_epoch = excluded.end_epoch,
            title = excluded.title, fetched_at = excluded.fetched_at
        """,
        (
            vod.id, vod.channel_id, vod.stream_id, vod.title, vod.created_at,
            vod.created_epoch, vod.duration_s, vod.end_epoch, vod.url, db.utcnow(),
        ),
    )


def _remember_alias(
    conn: sqlite3.Connection, player: Player, channel_id: str, source: str, confidence: float
) -> None:
    """Only ever called for a confirmed tie. An alias is permanent, so a wrong one
    is worse than none: Layer 2 returns it first and skips every later check."""
    conn.execute(
        """
        INSERT INTO alias (riot_name, riot_tag, channel_id, source, confidence, confirmed_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(riot_name, riot_tag, channel_id) DO UPDATE SET
            source = excluded.source,
            confidence = MAX(alias.confidence, excluded.confidence),
            confirmed_at = excluded.confirmed_at
        """,
        (player.name, player.tag, channel_id, source, confidence, db.utcnow()),
    )


def _remember_encounter(
    conn: sqlite3.Connection, match_id: str, cand: Candidate, same_team: bool
) -> None:
    conn.execute(
        """
        INSERT INTO encounter (match_id, puuid, riot_id, channel_id, channel_login,
                               vod_id, their_team, same_team, confidence, verified_how,
                               verdict, evidence, found_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(match_id, puuid, channel_id) DO UPDATE SET
            vod_id = excluded.vod_id, confidence = excluded.confidence,
            verified_how = excluded.verified_how, verdict = excluded.verdict,
            evidence = excluded.evidence
        """,
        (
            match_id, cand.player.puuid, f"{cand.player.name}#{cand.player.tag}",
            cand.channel_id, cand.channel_login, cand.vod.id if cand.vod else None,
            cand.player.team, int(same_team), cand.confidence, cand.source,
            cand.verdict, json.dumps(cand.evidence.__dict__), db.utcnow(),
        ),
    )


def _prefix_related(a: str, b: str, *, min_len: int = 3) -> bool:
    """Is one normalised name a prefix of the other? 'vae' <-> 'vaerie'.

    Far weaker than an exact match, so it only ever *associates* a channel that
    independently proved it was live and being clipped on Valorant in the window.
    """
    a, b = normalise(a), normalise(b)
    if len(a) < min_len or len(b) < min_len:
        return False
    return a.startswith(b) or b.startswith(a)


def gather_evidence(
    client: TwitchClient,
    conn: sqlite3.Connection,
    cand: Candidate,
    match: Match,
    start: int,
    end: int,
    game_id: str | None,
    report: ResolveReport,
) -> None:
    """Fill in cand.evidence: find a covering VOD, then ask what was on screen."""
    ev = cand.evidence
    try:
        vods = client.videos(cand.channel_id, limit=100)
    except Exception as exc:
        report.errors.append(f"{cand.channel_login}: videos failed: {exc}")
        return
    for vod in vods:
        _remember_vod(conn, vod)

    # The best broadcast is simply the one holding the most of the match.
    ev.match_len_s = max(end - start, 0)
    covering = max(vods, key=lambda v: v.overlap_s(start, end), default=None)
    if covering is None or covering.overlap_s(start, end) == 0:
        return
    cand.vod = covering
    ev.vod_covers = covering.covers(start, end, slack_s=VOD_SLACK_S)
    ev.vod_overlap_s = covering.overlap_s(start, end)
    ev.vod_late_start_s = max(covering.created_epoch - start, 0)
    ev.vod_early_end_s = max(end - covering.end_epoch, 0)
    ev.vod_title = covering.title
    if not ev.vod_usable:
        return

    # Layer 3 - does the channel publish a Riot ID anywhere we can read?
    texts = [v.title for v in vods[:20]]
    try:
        for user in client.users(ids=[cand.channel_id]):
            _remember_channel(conn, user)
            texts.append(user.get("description") or "")
    except Exception as exc:
        report.errors.append(f"{cand.channel_login}: users failed: {exc}")
    found: set[str] = set()
    for text in texts:
        found |= riot_ids_in(text)
    if found:
        ev.advertised_ids = tuple(sorted(found))
        if any(_same_id(rid, cand.player) for rid in found):
            ev.id_confirms = True
        elif normalise(cand.player.tag) not in _tags_in(ev.advertised_ids):
            # Every Riot ID this channel publishes belongs to a different tagline.
            ev.id_conflicts = True

    # What game was this broadcast? GET /helix/videos will not say, but clips cut
    # from it will: each names its source video, its offset and its category.
    if game_id:
        try:
            clips = client.channel_clips(
                cand.channel_id,
                _iso(covering.created_epoch),
                _iso(covering.end_epoch + CLIP_LOOKAHEAD_DAYS * 86400),
            )
        except Exception as exc:
            report.errors.append(f"{cand.channel_login}: clips failed: {exc}")
            return
        tied = [c for c in clips if str(c.get("video_id") or "") == covering.id]
        ev.clips_on_vod = len(tied)
        valorant = [c for c in tied if str(c.get("game_id") or "") == str(game_id)]
        ev.valorant_clips_on_vod = len(valorant)
        lo, hi = start - covering.created_epoch, end - covering.created_epoch
        inside = [
            c for c in valorant
            if c.get("vod_offset") is not None and lo <= int(c["vod_offset"]) <= hi
        ]
        ev.clips_inside_match = len(inside)
        map_name = (match.metadata.map_name or "").casefold()
        if map_name:
            ev.map_named_in_clip = any(
                map_name in (c.get("title") or "").casefold() for c in inside
            )


def clip_derived_candidates(
    conn: sqlite3.Connection,
    client: TwitchClient,
    match: Match,
    unresolved: list[Player],
    start: int,
    end: int,
    game_id: str,
    report: ResolveReport,
) -> list[Candidate]:
    """Layer 4 - candidates from clips made during the match window.

    GET /helix/clips takes game_id plus started_at/ended_at, so a couple of calls
    return every Valorant clip created worldwide in those minutes, each carrying a
    broadcaster. That is a candidate pool biased toward streamers with an audience,
    and it needs no name database at all.
    """
    if not unresolved:
        return []
    try:
        clips = client.clips_in_window(game_id, _iso(start), _iso(end + 60), limit=500)
    except Exception as exc:
        report.errors.append(f"clip window lookup failed: {exc}")
        return []

    report.clips_seen += len(clips)
    casters: dict[str, str] = {}
    display: dict[str, str] = {}
    for clip in clips:
        bid = clip.get("broadcaster_id")
        if bid:
            casters[bid] = clip.get("broadcaster_login") or clip.get("broadcaster_name") or ""
            display[bid] = clip.get("broadcaster_name") or ""
    report.clip_channels += len(casters)

    # Layer 3 over the clip pool. Prefix matching alone would hand the name problem
    # straight back: it only reaches channels that already look like the Riot ID. A
    # bio does not. /helix/users takes 100 ids at a time, so reading every candidate
    # channel's bio costs about two calls for a whole lobby, and a channel that was
    # being clipped on Valorant across the match AND publishes a lobby member's Riot
    # ID is tied to that player by something stronger than a resemblance.
    bios: dict[str, set[str]] = {}
    ids = list(casters)
    for start_i in range(0, len(ids), 100):
        try:
            for user in client.users(ids=ids[start_i:start_i + 100]):
                _remember_channel(conn, user)
                found = riot_ids_in(user.get("description") or "")
                if found:
                    bios[user["id"]] = found
        except Exception as exc:
            report.errors.append(f"clip-pool bios failed: {exc}")
            break
    report.bios_read += len(ids)

    out: list[Candidate] = []
    for bid, login in casters.items():
        player = next(
            (p for p in unresolved
             if any(_same_id(rid, p) for rid in bios.get(bid, ()))),
            None,
        )
        source = "clip_window+bio"
        if player is None:
            # Weaker: the channel name resembles the Riot ID. Only ever acted on
            # because the channel independently proved it was live on Valorant here.
            player = next(
                (p for p in unresolved
                 if _prefix_related(p.name, login) or _prefix_related(p.name, display.get(bid, ""))),
                None,
            )
            source = "clip_window"
        if player is None:
            continue
        cand = Candidate(match.id, player, bid, login, display.get(bid) or login, source)
        cand.evidence.name_prefix = source == "clip_window"
        _remember_channel(conn, {"id": bid, "broadcaster_login": login,
                                 "display_name": display.get(bid) or login})
        out.append(cand)
    return out


def resolve_match(
    conn: sqlite3.Connection,
    client: TwitchClient,
    match: Match,
    *,
    my_puuid: str,
    report: ResolveReport,
    game_id: str | None = None,
) -> None:
    started = match.metadata.started_at
    start = int(
        (started.replace(tzinfo=timezone.utc) if started.tzinfo is None else started).timestamp()
    )
    end = start + (match.metadata.game_length_in_ms or 0) // 1000

    me = match.player_by_puuid(my_puuid)
    my_team = me.team if me else None
    unresolved: list[Player] = []

    def finish(cand: Candidate) -> None:
        gather_evidence(client, conn, cand, match, start, end, game_id, report)
        _remember_encounter(conn, match.id, cand, cand.player.team == my_team)
        if cand.verdict == "confirmed":
            _remember_alias(conn, cand.player, cand.channel_id,
                            cand.source.split(":")[-1], cand.confidence)
        report.file(cand)

    for player in match.players:
        if player.puuid == my_puuid:
            continue
        report.players_checked += 1

        candidates: list[Candidate] = []

        # Layer 2 - anything already confirmed about this Riot ID.
        for row in _known_aliases(conn, player.name, player.tag):
            cand = Candidate(match.id, player, row["channel_id"], row["login"] or "",
                             row["display_name"] or "", f"alias:{row['source']}")
            cand.evidence.name_exact = normalise(row["login"] or "") == normalise(player.name)
            candidates.append(cand)

        # Layer 1 - Twitch name search. Zero database, catches the easy cases -
        # and collides with every established streamer who shares a short name.
        if not candidates:
            target = normalise(player.name)
            try:
                hits = client.search_channels(player.name, limit=20)
            except Exception as exc:
                report.errors.append(f"{player.name}#{player.tag}: search failed: {exc}")
                continue
            for hit in hits:
                login = hit.get("broadcaster_login", "")
                display = hit.get("display_name", "")
                if normalise(login) == target or normalise(display) == target:
                    _remember_channel(conn, hit)
                    cand = Candidate(match.id, player, hit["id"], login, display, "name_match")
                    cand.evidence.name_exact = True
                    candidates.append(cand)
            report.name_candidates += len(candidates)

        for cand in candidates:
            finish(cand)

        if not any(c.player.puuid == player.puuid for c in report.confirmed):
            unresolved.append(player)

    # Layer 4 - anyone name matching could not reach.
    if unresolved and game_id:
        for cand in clip_derived_candidates(
            conn, client, match, unresolved, start, end, game_id, report
        ):
            finish(cand)


def resolve_recent(cfg: Config, *, limit: int = 5, match_id: str | None = None) -> ResolveReport:
    report = ResolveReport()
    with db.session(cfg.db_path) as conn:
        if match_id:
            rows = conn.execute(
                "SELECT id, raw_json FROM match WHERE id LIKE ?", (f"{match_id}%",)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, raw_json FROM match ORDER BY started_at_epoch DESC LIMIT ?",
                (limit,),
            ).fetchall()

        if not rows:
            report.errors.append("No matching match in the database.")
            return report

        with TwitchClient(cfg.twitch_client_id, cfg.twitch_client_secret) as client:
            try:
                game_id = client.game_id("VALORANT")
            except Exception as exc:
                report.errors.append(f"game id lookup failed: {exc}")
                game_id = None
            for row in rows:
                match = Match.model_validate(json.loads(row["raw_json"]))
                me = match.find_me(cfg.riot_name, cfg.riot_tag)
                if me is None:
                    continue
                report.matches_checked += 1
                resolve_match(conn, client, match, my_puuid=me.puuid, report=report,
                              game_id=game_id)
                conn.commit()
    return report
