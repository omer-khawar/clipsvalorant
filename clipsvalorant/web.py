"""A local web UI: type a Riot ID, then watch the clips it finds.

Everything here runs on the machine that starts it. There is no login and no
account system because there is nothing to protect - the page reads a SQLite
file and embeds Twitch's own player.

Why the standard library rather than a web framework: the whole interface is
one page and five JSON endpoints, and `python -m clipsvalorant serve` working
on a fresh checkout with nothing extra installed is worth more than the
convenience of a framework.

The clip list is built entirely from the database. Opening the page costs no
API calls, so results found days ago are there instantly; only pressing Search
goes out to Riot and Twitch.

Two kinds of moment reach the page. Highlights are the few plays worth opening
with - multi-kills and clutches - and everything else is the rest of the
match: every round, and every kill inside it. Showing only the former was the
old behaviour, and it made a forty-minute match look like it contained four
things.
"""

from __future__ import annotations

import base64
import dataclasses
import hmac
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from clipsvalorant import calibrate, db
from clipsvalorant.assets import Assets
from clipsvalorant.cancel import Cancelled, CancelToken
from clipsvalorant.config import Config

log = logging.getLogger(__name__)

# Seconds of lead-in before each moment. A kill is the end of a play, not the
# start of it: landing exactly on the timestamp means arriving after the
# interesting part. Eight seconds is enough to see the approach.
LEAD_S = 8

# How far back a search reaches by default. Twitch keeps past broadcasts for
# 7 days, or 14 for Affiliates, Partners and Turbo subscribers, so fourteen is
# the whole of what can still be found; beyond it there is nothing left to
# link to. sweep.RETENTION_DAYS uses the same number for the same reason.
DEFAULT_DAYS = 14
MAX_DAYS = 30

# Matches pulled from HenrikDev in one search. The day window decides which of
# them are worth resolving; this is only a ceiling on the asking, so that a
# wide window cannot turn into an unbounded number of API calls.
#
# The ceiling used to be one hundred, which is also the largest page the
# listing endpoint returns - so the two limits coincided and thirty days
# quietly meant "the hundred most recent matches", which for an active player
# was twenty-one days. ingest.PAGE_SIZE is the page now and this is only the
# ceiling: 250 is thirty days at eight matches a day, more than anyone plays.
DEFAULT_FETCH = 40
MAX_FETCH = 250

MAX_JOB_LINES = 400

# A shared password, read from the environment at request time. Unset means no
# gate at all, which is what keeps `serve` on localhost exactly as it was;
# set means nothing but /healthz answers without it.
#
# The gate exists because the page has nothing else protecting it. Bound to
# localhost that is correct - there is no one else on the interface - but on a
# public hostname anyone who finds the URL is spending this machine's
# HenrikDev quota and holding the one search slot the whole server has.
#
# Basic auth rather than a login form: the browser then replays it on the
# page, the five JSON endpoints and the artwork alike, with nothing to store,
# no session to expire and no cookie to get wrong.
PASSWORD_ENV = "CLIPS_PASSWORD"

# The tab icon: the mark on the brand sheet's black tile. Inline rather than
# a file on disk because the whole UI is one module and a favicon is not
# worth a second deployable artefact; SVG rather than ICO because it stays
# sharp on every display and both colours come straight off the sheet.
FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    b'<rect width="32" height="32" rx="7" fill="#050505"/>'
    b'<g transform="translate(4 4)">'
    b'<path d="M3.2 9.4V3.2h6.2" fill="none" stroke="#fafafa" stroke-width="2.4"/>'
    b'<path d="M20.8 14.6v6.2h-6.2" fill="none" stroke="#fafafa" stroke-width="2.4"/>'
    b'<rect x="13.1" y="6.9" width="3.4" height="3.4" fill="#a97bff"/>'
    b'<rect x="8.3" y="10.3" width="3.4" height="3.4" fill="#a97bff"/>'
    b'<rect x="13.1" y="13.7" width="3.4" height="3.4" fill="#a97bff"/>'
    b"</g></svg>"
)

VERDICT_ORDER = {"confirmed": 0, "likely": 1, "unconfirmed": 2, "rejected": 3}

# Highlights are ranked for the "open with this" list. An ace beats a clutch
# beats a triple; ties fall back to the kill count, then to match order.
KIND_WEIGHT = {
    "ace": 0,
    "quadra": 1,
    "clutch": 2,
    "triple": 3,
    "clutch_lost": 4,
    "multi": 5,
}


# ---------------------------------------------------------------- accounts

def slug(riot_id: str) -> str:
    """A filename-safe stem for a Riot ID.

    Reduced to `[a-z0-9-]`, which is what keeps a Riot ID from being a path:
    a name like `../../etc/x` cannot survive this, and the UI passes whatever
    is typed into it straight through to a database filename.
    """
    out = re.sub(r"[^a-z0-9]+", "-", riot_id.lower()).strip("-")
    return out or "unnamed"


def split_riot_id(riot_id: str) -> tuple[str, str]:
    name, _, tag = riot_id.partition("#")
    name, tag = name.strip(), tag.strip().lstrip("#")
    if not name or not tag:
        raise ValueError(
            f"{riot_id!r} is not a Riot ID. It needs both halves, as in PlayerOne#NA1."
        )
    return name, tag


def config_for(cfg: Config, riot_id: str, *, region: str | None = None) -> Config:
    """A config aimed at one account, with a database of its own.

    Each account gets its own file. `is_me` is a column, not a query parameter -
    kills, highlights and the whole notion of "your" plays are derived from it
    at ingest time - so two accounts sharing one database would mean one of
    them silently owning the other's highlights.
    """
    name, tag = split_riot_id(riot_id)
    mine = (cfg.riot_name.lower(), cfg.riot_tag.lower())
    if (name.lower(), tag.lower()) == mine:
        path = cfg.db_path
    else:
        path = cfg.db_path.parent / f"ui-{slug(f'{name}-{tag}')}.db"
    return dataclasses.replace(
        cfg, riot_name=name, riot_tag=tag, db_path=path,
        region=(region or cfg.region).lower(),
    )


def _read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def known_accounts(data_dir: Path) -> list[dict[str, Any]]:
    """Every account already searched on this machine, newest first.

    Opened read-only on purpose. Listing is a side-effect-free act, and the
    schema migration that a normal connection runs would write to databases the
    user only wanted to look at.
    """
    out: list[dict[str, Any]] = []
    if not data_dir.is_dir():
        return out
    for path in sorted(data_dir.glob("*.db")):
        try:
            with _read_only(path) as conn:
                me = conn.execute(
                    "SELECT name, tag FROM riot_account WHERE is_me = 1 LIMIT 1"
                ).fetchone()
                counts = conn.execute(
                    "SELECT (SELECT COUNT(*) FROM match) AS matches,"
                    " (SELECT COUNT(*) FROM encounter WHERE verdict = 'confirmed')"
                    " AS confirmed"
                ).fetchone()
        except sqlite3.Error:
            continue  # not one of ours, or mid-write; it simply is not listed
        if me is None:
            continue
        out.append({
            "key": path.stem,
            "riot_id": f"{me['name']}#{me['tag']}",
            "matches": counts["matches"],
            "confirmed": counts["confirmed"],
            "updated": int(path.stat().st_mtime),
        })
    out.sort(key=lambda a: a["updated"], reverse=True)
    return out


def path_for_key(data_dir: Path, key: str) -> Path | None:
    """The database a browser asked for, or None.

    Checked against the listing rather than built from the key, so a crafted
    key cannot name a file outside the data directory.
    """
    for acct in known_accounts(data_dir):
        if acct["key"] == key:
            return data_dir / f"{key}.db"
    return None


# ---------------------------------------------------------------- clip list

def _evidence_reasons(blob: str | None) -> list[str]:
    """The stored evidence, read back as sentences.

    Tolerant of both directions of drift: a row written before a field existed,
    and a row written after one was removed. Evidence is the explanation shown
    next to a verdict, and an old row losing its explanation would be a worse
    outcome than a missing sentence.
    """
    if not blob:
        return []
    try:
        data = json.loads(blob)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    from clipsvalorant.resolve import Evidence

    known = {f.name for f in dataclasses.fields(Evidence)}
    kept = {k: v for k, v in data.items() if k in known}
    for key, value in list(kept.items()):
        if isinstance(value, list):  # tuples arrive from JSON as lists
            kept[key] = tuple(value)
    try:
        return Evidence(**kept).reasons()
    except (TypeError, ValueError):
        return []


_TIMELINE_SQL = """
SELECT round, time_in_match_ms, time_in_round_ms, is_mine,
       victim_name, victim_puuid, weapon
FROM kill
WHERE match_id = ?
ORDER BY time_in_match_ms ASC
"""


def match_timeline(conn: sqlite3.Connection, match_id: str) -> dict[str, Any]:
    """Every round of a match on the in-match clock, with your kills in it.

    Dated the way calibration dates a round: Riot puts both clocks on every
    kill, so `time_in_match_ms - time_in_round_ms` is the moment the barriers
    dropped.

    A round in which nobody died at all has no kill to carry those clocks and
    so does not appear here. It could be interpolated from its neighbours, but
    a guessed timestamp is exactly what this project just spent a release
    removing, and a round with no kills in it is not one anybody wants to
    re-watch.
    """
    me = conn.execute(
        "SELECT puuid FROM match_player WHERE match_id = ? AND is_me = 1 LIMIT 1",
        (match_id,),
    ).fetchone()
    my_puuid = me["puuid"] if me else ""

    rounds: dict[int, dict[str, Any]] = {}
    last_ms = 0
    for row in conn.execute(_TIMELINE_SQL, (match_id,)):
        rnd = int(row["round"])
        at = int(row["time_in_match_ms"])
        last_ms = max(last_ms, at)
        entry = rounds.get(rnd)
        if entry is None:
            start = at
            if row["time_in_round_ms"] is not None:
                start = at - int(row["time_in_round_ms"])
            entry = rounds[rnd] = {
                "round": rnd,
                "start_ms": max(int(start), 0),
                "kills": [],
                "died_ms": None,
            }
        if row["is_mine"]:
            entry["kills"].append({
                "ms": at,
                "victim": row["victim_name"] or "",
                "weapon": row["weapon"] or "",
            })
        if my_puuid and row["victim_puuid"] == my_puuid and entry["died_ms"] is None:
            entry["died_ms"] = at

    return {"rounds": [rounds[r] for r in sorted(rounds)], "last_ms": last_ms}


_CLIP_SQL = """
SELECT e.match_id, e.riot_id, e.channel_login, e.vod_id, e.verdict,
       e.same_team, e.evidence, e.verified_how,
       m.started_at_epoch, m.map, m.map_id, m.mode, m.queue,
       m.rounds_won, m.rounds_lost, m.my_team_won,
       c.profile_image_url, c.display_name AS channel_display,
       v.created_epoch, v.duration_s, v.title,
       h.round, h.label, h.kind, h.kill_count, h.time_in_match_ms, h.round_won,
       o.shift_s, o.method
FROM encounter e
JOIN match m      ON m.id = e.match_id
JOIN twitch_vod v ON v.id = e.vod_id
LEFT JOIN twitch_channel c ON c.id = e.channel_id
LEFT JOIN highlight h  ON h.match_id = e.match_id
LEFT JOIN match_vod_offset o
       ON o.match_id = e.match_id AND o.vod_id = e.vod_id
WHERE e.vod_id IS NOT NULL AND e.verdict IN ('confirmed', 'likely')
ORDER BY m.started_at_epoch DESC, h.time_in_match_ms ASC
"""
# That join is LEFT for a reason worth keeping. As an inner join, a match
# containing no highlight produced no rows at all, so the streamer vanished
# from the page entirely - not "a confirmed broadcast with nothing starred
# in it", but nothing. A 40-minute match where you took nine kills and no
# multi-kill is an ordinary match, and the round-by-round timeline attached
# below is exactly what you would want to watch of it.



def match_scoreboard(conn: sqlite3.Connection, match_id: str) -> list[dict[str, Any]]:
    """Both teams, best first, with enough to judge how the match went.

    Ordered by score within a team rather than by kills, because score is
    what Valorant's own scoreboard sorts on and a player who did the work
    without the frags should not drop to the bottom.
    """
    rows = conn.execute(
        """
        SELECT puuid, name, tag, team, agent, agent_id, tier, kills, deaths,
               assists, score, headshots, bodyshots, legshots,
               damage_dealt, damage_received, is_me
          FROM match_player
         WHERE match_id = ?
         ORDER BY is_me DESC, score DESC
        """,
        (match_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        shots = (row["headshots"] or 0) + (row["bodyshots"] or 0) + (row["legshots"] or 0)
        out.append({
            "puuid": row["puuid"],
            "name": row["name"],
            "tag": row["tag"],
            "team": row["team"] or "",
            "agent": row["agent"] or "",
            "agent_id": row["agent_id"] or "",
            "tier": row["tier"] or "",
            "kills": row["kills"] or 0,
            "deaths": row["deaths"] or 0,
            "assists": row["assists"] or 0,
            "score": row["score"] or 0,
            "damage": row["damage_dealt"] or 0,
            "hs": round((row["headshots"] or 0) / shots * 100) if shots else None,
            "mine": bool(row["is_me"]),
        })
    return out


def match_rounds(conn: sqlite3.Connection, match_id: str) -> dict[int, dict[str, Any]]:
    """How each round ended, keyed by round number.

    Separate from `match_timeline`, which dates rounds off the kill clock and
    therefore cannot see a round nobody died in. This one comes from Riot's
    own round list, so it has every round - including the quiet ones - and is
    what the timeline strip draws.
    """
    out: dict[int, dict[str, Any]] = {}
    me = conn.execute(
        "SELECT team FROM match_player WHERE match_id = ? AND is_me = 1 LIMIT 1",
        (match_id,),
    ).fetchone()
    my_team = (me["team"] if me else "") or ""
    for row in conn.execute(
        """
        SELECT round, result, winning_team, winning_role, ceremony,
               plant_ms, plant_site
          FROM round WHERE match_id = ? ORDER BY round
        """,
        (match_id,),
    ):
        winner = row["winning_team"] or ""
        out[int(row["round"])] = {
            "result": row["result"] or "",
            "winner": winner,
            "won": bool(my_team and winner and winner == my_team),
            "role": row["winning_role"] or "",
            "ceremony": row["ceremony"] or "",
            "planted": row["plant_ms"] is not None,
            "site": row["plant_site"] or "",
        }
    return out


def match_positions(
    conn: sqlite3.Connection, match_id: str, info: Any
) -> list[dict[str, Any]]:
    """Every kill of the match as a point on the minimap.

    `info` is an `assets.MapInfo`, which owns the conversion; a match whose
    map is unknown, or which was stored before coordinates were kept, simply
    returns nothing and the page draws no map rather than a wrong one.
    """
    if info is None or not info.usable:
        return []
    me = conn.execute(
        "SELECT puuid FROM match_player WHERE match_id = ? AND is_me = 1 LIMIT 1",
        (match_id,),
    ).fetchone()
    mine = me["puuid"] if me else ""
    out: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT round, time_in_match_ms, killer_name, victim_name, victim_puuid,
               weapon, is_mine, x, y
          FROM kill
         WHERE match_id = ? AND x IS NOT NULL AND y IS NOT NULL
         ORDER BY time_in_match_ms
        """,
        (match_id,),
    ):
        point = info.point(row["x"], row["y"])
        if point is None:
            continue
        died = bool(mine and row["victim_puuid"] == mine)
        out.append({
            "round": int(row["round"]),
            "ms": int(row["time_in_match_ms"]),
            "mx": round(point[0], 5),
            "my": round(point[1], 5),
            # Three kinds, because they are read differently: the ones you
            # got, the one that got you, and everyone else's, which are there
            # for context and drawn faintly.
            "kind": "mine" if row["is_mine"] else ("death" if died else "other"),
            "killer": row["killer_name"] or "",
            "victim": row["victim_name"] or "",
            "weapon": row["weapon"] or "",
        })
    return out


def _why_unplayable(at: int, duration: int) -> str:
    """Why a moment falls outside the broadcast, or '' when it is inside it."""
    if at < 0:
        return f"the stream began {abs(at)}s after this moment"
    if duration and at > duration:
        return f"the stream ended {at - duration}s before this moment"
    return ""


def _moment(
    *, match_start: int, vod_created: int, ms: int, shift: float,
    duration: int, lead: int,
) -> dict[str, Any]:
    """One clickable point: where it happens, and where to start playing.

    `at` is the moment itself and is what the scrubber draws; `offset` is
    where playback starts, which is `lead` seconds earlier. Keeping them
    apart is what lets a marker sit on the kill while the video still opens
    before it.
    """
    at = calibrate.moment_offset(
        match_start_epoch=match_start, vod_created_epoch=vod_created,
        time_in_match_ms=ms, shift_s=shift, lead_s=0,
    )
    seek = max(at - lead, 0)
    why = _why_unplayable(at, duration)
    return {
        "at": at,
        "offset": seek,
        "stamp": calibrate.stamp(seek),
        "playable": not why,
        "why_not": why,
    }


def clip_groups(
    conn: sqlite3.Connection, *, lead: int = LEAD_S, assets: Any = None,
) -> list[dict[str, Any]]:
    """One group per (match, streamer), holding everything playable in it.

    A moment is only playable if the broadcast actually contains it. Two
    things can put it outside: a stream that began after the moment, and one
    that ended before it. Both are reported rather than dropped - a streamer
    who joined at round 14 is a real find, and silently showing nothing for
    them looks identical to a bug.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    created: dict[str, int] = {}

    for row in conn.execute(_CLIP_SQL):
        key = (row["match_id"], row["channel_login"])
        created.setdefault(row["vod_id"], int(row["created_epoch"]))
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "id": f"{row['match_id']}:{row['channel_login']}",
                "match_id": row["match_id"],
                "riot_id": row["riot_id"],
                "channel": row["channel_login"],
                "vod_id": row["vod_id"],
                "verdict": row["verdict"],
                "same_team": bool(row["same_team"]),
                "source": row["verified_how"] or "",
                "map": row["map"] or "?",
                "map_id": row["map_id"] or "",
                "avatar": row["profile_image_url"] or "",
                "channel_name": row["channel_display"] or row["channel_login"],
                "mode": row["mode"] or row["queue"] or "?",
                "started": row["started_at_epoch"],
                "started_text": datetime.fromtimestamp(
                    row["started_at_epoch"], timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC"),
                "score": (
                    f"{row['rounds_won']}-{row['rounds_lost']}"
                    if row["rounds_won"] is not None else ""
                ),
                "won": None if row["my_team_won"] is None else bool(row["my_team_won"]),
                "vod_title": row["title"] or "",
                "vod_duration": row["duration_s"] or 0,
                "reasons": _evidence_reasons(row["evidence"]),
                "timing": row["method"] or "estimated",
                "shift_s": row["shift_s"] if row["shift_s"] is not None
                           else calibrate.FALLBACK_SHIFT_S,
                "clips": [],
                "rounds": [],
            }

        # No highlight in this match: the group still exists, and gets its
        # rounds and kills from the timeline attached further down.
        if row["time_in_match_ms"] is None:
            continue

        moment = _moment(
            match_start=row["started_at_epoch"], vod_created=row["created_epoch"],
            ms=row["time_in_match_ms"], shift=group["shift_s"],
            duration=row["duration_s"] or 0, lead=lead,
        )
        moment.update({
            "round": row["round"],
            "label": row["label"],
            "kind": row["kind"],
            "kills": row["kill_count"],
            "won": None if row["round_won"] is None else bool(row["round_won"]),
            "url": f"https://www.twitch.tv/videos/{row['vod_id']}?t={moment['stamp']}",
        })
        group["clips"].append(moment)

    # The round-by-round timeline is a property of the match, not of who was
    # broadcasting it, so it is read once per match and shared by every
    # streamer in that lobby.
    timelines: dict[str, dict[str, Any]] = {}
    detail: dict[str, dict[str, Any]] = {}
    for group in groups.values():
        match_id = group["match_id"]
        if match_id not in timelines:
            timelines[match_id] = match_timeline(conn, match_id)
        _attach_timeline(
            group, timelines[match_id], created.get(group["vod_id"], 0), lead=lead
        )
        # The scoreboard, the round outcomes and the kill positions belong to
        # the match, not to whoever was broadcasting it, so each is read once
        # however many streamers were in that lobby.
        if match_id not in detail:
            info = assets.map_for(group["map_id"], group["map"]) if assets else None
            detail[match_id] = {
                "scoreboard": match_scoreboard(conn, match_id),
                "outcomes": match_rounds(conn, match_id),
                "positions": match_positions(conn, match_id, info),
                "minimap": bool(info and info.usable),
            }
        got = detail[match_id]
        group["scoreboard"] = got["scoreboard"]
        group["minimap"] = got["minimap"]
        # Each dot carries its own seek point, so a kill on the map is a
        # click away from the moment it happened. The positions themselves
        # belong to the match, but where they land inside a broadcast is
        # different for every streamer in that lobby, so each gets a copy.
        group["positions"] = [
            dict(pos, **_moment(
                match_start=group["started"],
                vod_created=created.get(group["vod_id"], 0),
                ms=pos["ms"], shift=group["shift_s"],
                duration=group["vod_duration"], lead=lead,
            ))
            for pos in got["positions"]
        ]
        me = next((p for p in got["scoreboard"] if p["mine"]), None)
        group["me"] = me or {}
        for entry in group["rounds"]:
            outcome = got["outcomes"].get(entry["round"])
            if outcome:
                entry.update(outcome)

    out = list(groups.values())
    out.sort(key=lambda g: (VERDICT_ORDER.get(g["verdict"], 9), -g["started"]))
    return out


def _attach_timeline(
    group: dict[str, Any], timeline: dict[str, Any], vod_created: int, *, lead: int
) -> None:
    """Hang every round, kill and death of the match off one streamer's group."""
    duration = group["vod_duration"]
    shift = group["shift_s"]
    start = group["started"]

    def point(ms: int, lead_s: int) -> dict[str, Any]:
        return _moment(
            match_start=start, vod_created=vod_created, ms=ms,
            shift=shift, duration=duration, lead=lead_s,
        )

    rounds: list[dict[str, Any]] = []
    for entry in timeline["rounds"]:
        # A round starts where it starts - no lead-in, or every round would
        # open during the previous one's aftermath.
        moment = point(entry["start_ms"], 0)
        kills = []
        for kill in entry["kills"]:
            mark = point(kill["ms"], lead)
            mark.update({
                "victim": kill["victim"],
                "weapon": kill["weapon"],
                "round": entry["round"],
            })
            kills.append(mark)
        moment.update({
            "round": entry["round"],
            "kills": kills,
            "died": point(entry["died_ms"], 0)["at"]
                    if entry["died_ms"] is not None else None,
        })
        rounds.append(moment)

    group["rounds"] = rounds
    group["clips"].sort(
        key=lambda c: (KIND_WEIGHT.get(c["kind"], 9), -(c["kills"] or 0), c["at"])
    )
    group["playable"] = sum(1 for c in group["clips"] if c["playable"])
    group["kill_count"] = sum(len(r["kills"]) for r in rounds)

    first = rounds[0]["at"] if rounds else (
        group["clips"][0]["at"] if group["clips"] else 0
    )
    last = point(timeline["last_ms"], 0)["at"] if timeline["last_ms"] else first
    group["span"] = {
        "start": first,
        "end": max(last, first + 1),
        "duration": duration,
    }


def account_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM match) AS matches,"
        " (SELECT COUNT(*) FROM highlight) AS highlights,"
        " (SELECT COUNT(*) FROM encounter) AS encounters,"
        " (SELECT COUNT(*) FROM encounter WHERE verdict = 'rejected') AS rejected,"
        " (SELECT COUNT(*) FROM encounter"
        "  WHERE verdict IN ('confirmed', 'likely')) AS found,"
        # Whose account this is, and which shard it actually plays on -
        # written at ingest from HenrikDev's own answer, not from whatever
        # region the server happens to be configured for.
        " (SELECT region FROM riot_account WHERE is_me = 1 LIMIT 1) AS region,"
        " (SELECT name || '#' || tag FROM riot_account WHERE is_me = 1 LIMIT 1)"
        "   AS riot_id"
    ).fetchone()
    return dict(row)


# ---------------------------------------------------------------- jobs

@dataclass
class Job:
    """One search, running on its own thread, watched by polling."""

    id: str
    riot_id: str
    key: str
    state: str = "running"  # queued | running | done | error | cancelled
    phase: str = "starting"  # starting | ingest | resolve | done
    done: int = 0
    total: int = 0
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    # Who asked, and what they asked for. Both belong to the queue rather
    # than to the scan itself: the first is how "your scan" is told apart
    # from somebody else's with nobody logging in, and the second is what
    # the worker replays when the turn finally comes round.
    client: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    queued_at: float = 0.0
    error: str = ""
    lines: list[str] = field(default_factory=list)
    # Said out loud, under the progress bar. The log is for afterwards; this
    # is for the two or three things that change what you should expect from
    # the run you are watching - a corrected region, a window you did not
    # actually get.
    notes: list[str] = field(default_factory=list)
    token: CancelToken = field(default_factory=CancelToken, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def note(self, line: str) -> None:
        """Say it in the log and keep it in front of the user."""
        with self._lock:
            if line not in self.notes:
                self.notes.append(line)
        self.say(line)

    def stop(self) -> None:
        """Ask this scan to stop, from whichever thread is asking.

        Only sets a flag. The scan notices between matches and inside every
        wait, and what it has already written to the database stays written.
        """
        self.token.cancel()
        self.say("Stopping...")

    def say(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > MAX_JOB_LINES:
                del self.lines[: len(self.lines) - MAX_JOB_LINES]
        log.info("%s", line)

    def step(
        self, phase: str, *, done: int | None = None, total: int | None = None
    ) -> None:
        """Move the progress bar.

        Separate from `say` because a narration line and a position are
        different things: the log is for reading afterwards, this is for
        watching while it runs.
        """
        with self._lock:
            self.phase = phase
            if done is not None:
                self.done = done
            if total is not None:
                self.total = total

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            lines = list(self.lines)
            notes = list(self.notes)
            phase, done, total = self.phase, self.done, self.total
        return {
            "id": self.id,
            "riot_id": self.riot_id,
            "key": self.key,
            "state": self.state,
            "stopping": self.token.cancelled and self.state == "running",
            "phase": phase,
            "done": done,
            "total": total,
            "error": self.error,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "notes": notes,
            "lines": lines,
        }


class _JobLog(logging.Handler):
    """Package log records, relayed into the job the user is watching.

    Attached at INFO, which is also what keeps credentials out of the page:
    the only records that carry request URLs are DEBUG.
    """

    def __init__(self, job: Job) -> None:
        super().__init__(level=logging.INFO)
        self._job = job

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("clipsvalorant.web"):
            return  # Job.say already logs; relaying it back would double every line
        try:
            self._job.say(record.getMessage())
        except Exception:  # pragma: no cover - a logging handler must not raise
            pass


def _how_long(seconds: float) -> str:
    """A wait, in the roundest words that are still true.

    Deliberately coarse. The estimate it dresses is an average over a
    handful of matches, and quoting it to the second would claim a
    precision the number does not have.
    """
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{max(1, int(round(seconds)))}s"
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h" if rest == 0 else f"{hours}h {rest}m"


def _correct_region(acct: Config, job: Job) -> Config:
    """Ask HenrikDev which shard this Riot ID actually plays on.

    Returns the config to use. On any doubt - the lookup failing, an
    unfamiliar answer - the requested region is kept: a scan that runs on
    the region you picked is a normal outcome, while one silently moved to
    a region nobody asked for is a surprise.
    """
    from clipsvalorant.henrik import HenrikClient

    if not acct.henrik_api_key:
        return acct  # nothing to ask with; the dropdown is all we have

    try:
        with HenrikClient(acct.henrik_api_key, cancel=job.token) as client:
            info = client.account(acct.riot_name, acct.riot_tag)
    except Cancelled:
        raise
    except Exception as exc:
        job.say(f"Could not confirm which region this account plays on ({exc}).")
        return acct

    real = str(info.get("region") or "").strip().lower()
    if not real or real == acct.region.lower():
        return acct
    job.note(
        f"{acct.riot_id} plays on {real.upper()}, not {acct.region.upper()} - "
        f"searching {real.upper()} instead."
    )
    return config_for(acct, acct.riot_id, region=real)


def run_search(
    cfg: Config,
    job: Job,
    *,
    days: int = DEFAULT_DAYS,
    fetch: int = DEFAULT_FETCH,
    mode: str | None = "competitive",
    watch: bool = True,
    region: str | None = None,
) -> None:
    """Ingest then resolve one account, narrating as it goes.

    `days` is the window that matters: Twitch deletes broadcasts after one or
    two weeks, so a match older than that has nothing left to link to however
    well its streamers are identified. `fetch` only caps how many matches are
    asked for, which is what stops a wide window becoming unbounded API calls.
    """
    from clipsvalorant.ingest import ingest, other_modes
    from clipsvalorant.resolve import resolve_recent
    from clipsvalorant.henrik import HenrikClient, HenrikError

    days = max(1, min(MAX_DAYS, int(days)))
    fetch = max(1, min(MAX_FETCH, int(fetch)))

    acct = config_for(cfg, job.riot_id, region=region)
    job.key = acct.db_path.stem
    handler = _JobLog(job)
    package = logging.getLogger("clipsvalorant")
    package.addHandler(handler)
    previous = package.level
    package.setLevel(logging.INFO)
    try:
        cutoff = int(time.time()) - days * 86400

        job.step("ingest")
        job.say(f"Looking up {acct.riot_id} in {acct.region.upper()}...")

        # One cheap call, and it is what stops a whole scan being wasted.
        # A Riot ID is global but matches are not, and the stored-matches
        # endpoint does not object when you ask the wrong shard about
        # somebody - it answers, with their real matches, which is how a
        # European player searched on NA runs for seven minutes and files
        # forty matches played in Frankfurt. So we ask who they are first
        # and believe that over the dropdown.
        acct = _correct_region(acct, job)
        # The window is handed to ingest, not applied after it. A match
        # outside it is never going to be resolved, and its detail call is
        # the most expensive thing in the whole run.
        report = ingest(acct, limit=fetch, mode=mode, since_epoch=cutoff,
                        cancel=job.token)
        job.say(report.summary())
        for err in report.errors[:5]:
            job.say(f"! {err}")

        # What the queue filter is holding back. One listing call, and it is
        # the difference between "45 matches" and "45 of 69": a scan fixed to
        # Competitive can miss a third of a fortnight, and a match played in
        # Swiftplay or a custom game looks from the outside exactly like a
        # scanner that failed to find anybody.
        if mode:
            try:
                skipped = other_modes(acct, mode=mode, since_epoch=cutoff,
                                      limit=fetch, cancel=job.token)
            except Cancelled:
                raise
            except Exception as exc:  # a footnote must not fail a scan
                log.debug("Could not count the other queues: %s", exc)
                skipped = None
            if skipped:
                spread = ", ".join(f"{name} {n}" for name, n in skipped.most_common())
                job.say(
                    f"{mode.capitalize()} only - {sum(skipped.values())} other "
                    f"match(es) in this window were not looked at ({spread}). "
                    "Switch the queue to All to include them."
                )
        with db.session(acct.db_path) as conn:
            ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM match WHERE started_at_epoch >= ?"
                    " ORDER BY started_at_epoch DESC",
                    (cutoff,),
                )
            ]
            edges = conn.execute(
                "SELECT MAX(started_at_epoch) AS newest FROM match"
            ).fetchone()
            newest = edges["newest"]

        if not ids:
            if newest:
                age = int((time.time() - newest) / 86400)
                job.say(
                    f"Nothing inside the last {days} days. The newest match stored "
                    f"for this account is {age} days old, and Twitch has already "
                    "deleted any broadcast of it."
                )
            else:
                job.say(
                    "No matches stored for this account. HenrikDev only keeps "
                    "matches somebody has already asked it for, so a brand new "
                    "name can come back empty - try another queue, or play a "
                    "game and search again."
                )
            job.step("done", done=0, total=0)
            job.state = "done"
            return

        # Whether the window was covered is ingest's answer to give, not
        # something to infer from how full the listing came back: it is the
        # half that walks the pages and the only half that knows whether it
        # stopped because the history ran out or because the ceiling did.
        if not report.complete:
            reach = report.oldest_listed
            covered = (time.time() - reach) / 86400 if reach else 0.0
            job.note(
                f"Stopped at the {fetch}-match ceiling {covered:.1f} days back, so "
                f"this is the most recent {report.seen} matches and not the full "
                f"{days} days - older matches inside the window were never looked "
                "at. A shorter window gets covered in full."
            )

        if not acct.has_twitch:
            job.say(
                "Twitch credentials are not configured, so the streamer search "
                "cannot run. Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env."
            )
            job.step("done", done=0, total=0)
            job.state = "done"
            return

        job.step("resolve", done=0, total=len(ids))
        job.say(
            f"Checking {len(ids)} match(es) from the last {days} days for players "
            "who were streaming"
            + (" (reading the broadcasts, which is the slow part)" if watch else "")
            + "..."
        )
        done = 0
        began = time.time()

        def tick(match_id: str, report: Any) -> None:
            nonlocal done
            done += 1
            job.step("resolve", done=done)
            # The pace this machine is actually managing, not a constant
            # measured on another one. A match costs about 4.7s on a laptop
            # and about 34s on a 1/8 OCPU box, so the only figure worth
            # showing is the one this run is setting, and a wide window on
            # slow hardware is an hour nobody was warned about.
            left = ""
            if 3 <= done < len(ids):
                pace = (time.time() - began) / done
                left = f", about {_how_long(pace * (len(ids) - done))} left"
            job.say(
                f"match {done}/{len(ids)} checked - "
                f"{len(report.confirmed)} confirmed, {len(report.likely)} likely, "
                f"{len(report.unverified)} unverified{left}"
            )

        found = resolve_recent(
            acct, match_ids=ids, watch_vods=watch, on_match=tick,
            cancel=job.token,
        )
        job.say(found.summary())
        for err in found.errors[:5]:
            job.say(f"! {err}")
        job.step("done", done=len(ids), total=len(ids))
        job.state = "done"
    except Cancelled:
        # Not a failure. Everything already written stays written, and the
        # page reloads what was found before the stop.
        job.state = "cancelled"
        job.step("done")
        job.say("Stopped. Anything already found has been kept.")
    except Exception as exc:  # the thread is the only place this can be caught
        job.state = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.say(f"Failed: {job.error}")
        log.exception("Search for %s failed", job.riot_id)
    finally:
        job.finished = time.time()
        package.removeHandler(handler)
        package.setLevel(previous)


# ---------------------------------------------------------------- the queue

# How many scans run at once. One, because everything downstream is rate
# limited by a single HenrikDev key: two scans do not finish in half the
# time, they share the same requests per minute and both crawl, and the
# resolver's own ffmpeg workers already saturate a small machine between
# them. Serialising was always the behaviour - the queue is what makes it
# legible, "you are third", instead of a search that mysteriously refuses to
# start.
WORKERS = 1

# A ceiling on the line. Past this, the wait is longer than anyone will sit
# through, and saying so at once is kinder than handing out a position
# nobody reaches.
MAX_WAITING = 24

# How long a finished scan stays readable after it ends. Long enough to
# reload the page and still find what it did, short enough that a busy
# evening does not accumulate in memory.
KEEP_DONE_S = 20 * 60

# What a scan tends to cost, used only to turn a position into a number of
# minutes. This was guessed at two to five minutes and was wrong by a factor
# of four, which meant the queue told the fourth person in line to expect a
# quarter of an hour and then kept them an hour.
#
# Measured instead, from the timestamps a scan leaves behind - ingested_at
# and last_resolved_at across one full account: 100 matches cost 341s to
# ingest and 461s to resolve, so 3.4s and 4.7s a match, 800s together. The
# resolve half is the cheaper one per match despite being the half that
# opens broadcasts, because Layer 5 is selective: that same scan read 292
# frames in total, against 4,104 candidates it judged on cheaper evidence.
#
# Rounded up rather than down. A line that overstates the wait is kinder
# than one that understates it, and a full scan is the case worth quoting:
# a quieter account finishes sooner and nobody minds.
TYPICAL_SCAN_S = 800


class Queue:
    """Scans waiting their turn, and the worker thread that runs them.

    The old behaviour - start on arrival, refuse if busy - was right for one
    person on localhost and fails quietly for four people on a URL. Refusing
    tells the second person to try again with no idea when; running both
    would split one API key's quota and make each scan slower than if they
    had taken turns. A queue is the honest version of what the rate limit
    already forces.

    Everything here is in memory, deliberately. A queue that outlived a
    restart would hand people back a position in a line whose running scan
    died with the process, which is worse than being told to ask again.
    """

    def __init__(
        self,
        cfg: Config,
        runner: Callable[..., None],
        workers: int = WORKERS,
    ) -> None:
        self.cfg = cfg
        self.runner = runner
        self._cv = threading.Condition()
        self._waiting: deque[Job] = deque()
        self._running: dict[str, Job] = {}
        self._finished: dict[str, Job] = {}
        self._closed = False
        self._threads = [
            threading.Thread(target=self._work, daemon=True, name=f"scan-{n}")
            for n in range(max(1, workers))
        ]
        for thread in self._threads:
            thread.start()

    # -- asking

    def submit(self, riot_id: str, *, client: str = "", **options: Any) -> Job:
        """Join the line, or raise RuntimeError saying why not."""
        with self._cv:
            self._sweep()
            mine = self._active_for(client)
            if mine is not None:
                raise RuntimeError(
                    f"You already have a scan {mine.state} for {mine.riot_id}."
                )
            if len(self._waiting) >= MAX_WAITING:
                raise RuntimeError(
                    f"{len(self._waiting)} scans are already waiting. "
                    "Try again in a few minutes."
                )
            # An unguessable id, because it is the only thing standing
            # between one person's scan and another person's cancel button.
            job = Job(
                id=secrets.token_urlsafe(9),
                riot_id=riot_id,
                key="",
                state="queued",
                client=client,
                options=dict(options),
                queued_at=time.time(),
            )
            self._waiting.append(job)
            self._cv.notify()
            return job

    def cancel(self, ticket: str, *, client: str = "") -> Job:
        """Drop a scan, whether it is waiting or already running."""
        with self._cv:
            job = self._find(ticket)
            if job is None:
                raise KeyError(ticket)
            if job.client and client and job.client != client:
                raise PermissionError(ticket)
            if job.state == "queued":
                job.state = "cancelled"
                job.finished = time.time()
                try:
                    self._waiting.remove(job)
                except ValueError:  # pragma: no cover - a worker just took it
                    pass
                self._finished[job.id] = job
                return job
        # Outside the lock: stopping narrates into the job, and the worker
        # thread holds nothing of ours while it runs.
        if job.state == "running":
            job.stop()
        return job

    # -- looking

    def snapshot(self, ticket: str) -> dict[str, Any]:
        """One client's scan, with its place in the line if it is waiting."""
        with self._cv:
            self._sweep()
            job = self._find(ticket)
            if job is None:
                # Not an error. A ticket from before a restart, or one that
                # has aged out, should put the page back to a usable state
                # rather than show it a failure it can do nothing about.
                return {"state": "unknown"}
            snap = job.snapshot()
            if job.state == "queued":
                ahead = 0
                for other in self._waiting:
                    if other is job:
                        break
                    ahead += 1
                snap["position"] = ahead + 1
                snap["ahead"] = ahead
                snap["waiting"] = round(time.time() - job.queued_at, 1)
                snap["eta"] = int((ahead + len(self._running)) * TYPICAL_SCAN_S)
                # What is happening instead of your scan. Carried on the
                # same poll so the waiting screen needs one request rather
                # than two, and so a queue that is moving looks like it.
                snap["now"] = [
                    {
                        "riot_id": other.riot_id,
                        "phase": other.phase,
                        "done": other.done,
                        "total": other.total,
                    }
                    for other in self._running.values()
                ]
            return snap

    def stats(self) -> dict[str, Any]:
        """The state of the line, for anyone looking, ticket or not."""
        with self._cv:
            self._sweep()
            running = [
                {
                    "riot_id": job.riot_id,
                    "phase": job.phase,
                    "done": job.done,
                    "total": job.total,
                    "elapsed": round(time.time() - job.started, 1),
                }
                for job in self._running.values()
            ]
            return {
                "running": running,
                "waiting": len(self._waiting),
                "workers": len(self._threads),
                "max_waiting": MAX_WAITING,
                "typical_scan_s": TYPICAL_SCAN_S,
            }

    def close(self) -> None:
        """Let the workers finish. For tests and for a tidy shutdown."""
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    # -- the worker

    def _work(self) -> None:
        while True:
            with self._cv:
                while not self._waiting and not self._closed:
                    self._cv.wait()
                if self._closed and not self._waiting:
                    return
                job = self._waiting.popleft()
                if job.state != "queued":  # cancelled while it waited
                    continue
                job.state = "running"
                job.started = time.time()
                self._running[job.id] = job
            try:
                self.runner(self.cfg, job, **job.options)
            except Exception as exc:  # the worker is the last line of defence
                job.state = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                log.exception("Queued search for %s failed", job.riot_id)
            finally:
                # A runner that returned without saying how it went still
                # has to leave the queue, or the next person waits forever.
                if job.state == "running":
                    job.state = "done"
                if not job.finished:
                    job.finished = time.time()
                with self._cv:
                    self._running.pop(job.id, None)
                    self._finished[job.id] = job

    # -- bookkeeping, all called with the lock held

    def _find(self, ticket: str) -> Job | None:
        if not ticket:
            return None
        job = self._running.get(ticket) or self._finished.get(ticket)
        if job is not None:
            return job
        for waiting in self._waiting:
            if waiting.id == ticket:
                return waiting
        return None

    def _active_for(self, client: str) -> Job | None:
        """The scan this client already has in flight, if any.

        One at a time per person, which is what stops a single browser - or
        a single impatient refresh - from filling a queue everyone shares.
        """
        if not client:
            return None
        for job in list(self._running.values()) + list(self._waiting):
            if job.client == client:
                return job
        return None

    def _sweep(self) -> None:
        cutoff = time.time() - KEEP_DONE_S
        stale = [
            ticket
            for ticket, job in self._finished.items()
            if job.finished and job.finished < cutoff
        ]
        for ticket in stale:
            del self._finished[ticket]


@dataclass
class State:
    """What the handler is allowed to touch."""

    cfg: Config
    runner: Callable[..., None] = run_search
    _assets: Any = None
    _queue: Queue | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def assets(self) -> Assets:
        if self._assets is None:
            self._assets = Assets(self.cfg.db_path.parent / "assets")
        return self._assets

    @property
    def data_dir(self) -> Path:
        return self.cfg.db_path.parent

    @property
    def queue(self) -> Queue:
        """Built on first use, so a page nobody searches on starts no threads."""
        with self._lock:
            if self._queue is None:
                self._queue = Queue(self.cfg, self.runner)
            return self._queue

    def close(self) -> None:
        if self._queue is not None:
            self._queue.close()


# ---------------------------------------------------------------- HTTP

def make_handler(state: State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "clipsvalorant"
        protocol_version = "HTTP/1.1"

        # -- plumbing

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload: Any, code: int = 200) -> None:
            self._send(
                code,
                json.dumps(payload).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw)
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        # -- who is asking

        def _client(self) -> str:
            """Who is asking, as well as anything here can know.

            There are no accounts, so "one scan at a time per person" has to
            mean per address. Fly's proxy puts the real one in Fly-Client-IP;
            behind anything else the first entry of X-Forwarded-For is the
            usual convention, and on localhost it is simply the socket.

            Both headers are client-settable, so this is a courtesy rather
            than a boundary: it stops an impatient refresh from filling a
            shared queue, not a determined person from trying.
            """
            for header in ("Fly-Client-IP", "X-Forwarded-For"):
                raw = self.headers.get(header, "")
                if raw.strip():
                    return raw.split(",")[0].strip()[:64]
            try:
                return str(self.client_address[0])
            except (AttributeError, IndexError):  # pragma: no cover
                return ""

        # -- the gate

        def _authorized(self) -> bool:
            """True when this request carries the configured password, or there isn't one."""
            secret = os.environ.get(PASSWORD_ENV, "").strip()
            if not secret:
                return True
            scheme, _, token = self.headers.get("Authorization", "").partition(" ")
            if scheme.lower() != "basic":
                return False
            try:
                decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return False
            # Any username; the password is the whole secret. One field is
            # less to mistype on a phone, and there are no accounts here to
            # tell apart.
            _, _, offered = decoded.partition(":")
            return hmac.compare_digest(offered, secret)

        def _ask_for_password(self) -> None:
            body = b"clipsvalorant: password required\n"
            self.send_response(401)
            self.send_header(
                "WWW-Authenticate", 'Basic realm="clipsvalorant", charset="UTF-8"'
            )
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        # -- routes

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
            url = urlparse(self.path)
            query = parse_qs(url.query)
            route = url.path.rstrip("/") or "/"
            # Outside the gate deliberately: a host's health check has no
            # password to offer, and a 401 would read as a dead machine and
            # get it restarted under a running scan.
            if route == "/healthz":
                self._send(200, b"ok\n", "text/plain; charset=utf-8")
                return
            # Also outside the gate: a tab icon gives nothing away, and a
            # browser asks for it before anyone has typed a password.
            if route == "/favicon.svg":
                self._send(200, FAVICON, "image/svg+xml")
                return
            if not self._authorized():
                self._ask_for_password()
                return
            try:
                if route == "/":
                    self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif route == "/api/state":
                    self._json(self._state_payload())
                elif route == "/api/clips":
                    self._json(self._clips_payload(query.get("key", [""])[0]))
                elif route.startswith("/api/icon/"):
                    self._icon(route)
                elif route == "/api/job":
                    self._json(state.queue.snapshot(query.get("ticket", [""])[0]))
                elif route == "/api/queue":
                    self._json(state.queue.stats())
                else:
                    self._json({"error": "no such endpoint"}, 404)
            except Exception as exc:
                log.exception("GET %s failed", self.path)
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        do_HEAD = do_GET

        def do_POST(self) -> None:  # noqa: N802
            route = urlparse(self.path).path.rstrip("/") or "/"
            if not self._authorized():
                self._ask_for_password()
                return
            if route == "/api/cancel":
                ticket = str(self._body().get("ticket") or "").strip()
                try:
                    state.queue.cancel(ticket, client=self._client())
                except KeyError:
                    self._json({"error": "that scan is not in the queue"}, 404)
                    return
                except PermissionError:
                    self._json({"error": "that scan belongs to someone else"}, 403)
                    return
                self._json(state.queue.snapshot(ticket), 202)
                return
            if route != "/api/search":
                self._json({"error": "no such endpoint"}, 404)
                return
            body = self._body()
            riot_id = str(body.get("riot_id") or "").strip()
            try:
                split_riot_id(riot_id)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            try:
                days = max(1, min(MAX_DAYS, int(body.get("days") or DEFAULT_DAYS)))
            except (TypeError, ValueError):
                days = DEFAULT_DAYS
            try:
                fetch = max(1, min(MAX_FETCH, int(body.get("fetch") or DEFAULT_FETCH)))
            except (TypeError, ValueError):
                fetch = DEFAULT_FETCH
            mode = str(body.get("mode") or "").strip().lower() or None
            if mode == "any":
                mode = None
            region = str(body.get("region") or "").strip().lower() or None
            try:
                job = state.queue.submit(
                    riot_id,
                    client=self._client(),
                    days=days,
                    fetch=fetch,
                    mode=mode,
                    watch=bool(body.get("watch", True)),
                    region=region,
                )
            except RuntimeError as exc:
                self._json({"error": str(exc)}, 409)
                return
            self._json(state.queue.snapshot(job.id), 202)

        # -- payloads

        def _state_payload(self) -> dict[str, Any]:
            cfg = state.cfg
            return {
                "default_riot_id": cfg.riot_id,
                "default_key": cfg.db_path.stem,
                "default_days": DEFAULT_DAYS,
                "max_days": MAX_DAYS,
                "region": cfg.region,
                "has_twitch": cfg.has_twitch,
                "accounts": known_accounts(state.data_dir),
                "queue": state.queue.stats(),
            }

        def _icon(self, route: str) -> None:
            """Map and agent artwork, served from our own cache.

            Deliberately proxied rather than linked. Pointing the page at
            media.valorant-api.com would hand that CDN the viewer's address
            and the list of maps and agents they play, every time the page is
            opened, for a tool that otherwise speaks only to localhost and
            Twitch. Cached copies also mean the page still draws with no
            network at all.
            """
            parts = route[len("/api/icon/"):].split("/")
            if len(parts) != 2:
                self._json({"error": "no such icon"}, 404)
                return
            kind, name = parts
            path = state.assets.image(kind, name.removesuffix(".png"))
            if path is None:
                self._json({"error": "no artwork"}, 404)
                return
            try:
                body = path.read_bytes()
            except OSError:
                self._json({"error": "no artwork"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            # Immutable by nature: these are keyed by Riot's own asset uuid.
            self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _clips_payload(self, key: str) -> dict[str, Any]:
            key = key or state.cfg.db_path.stem
            path = path_for_key(state.data_dir, key)
            if path is None:
                return {"key": key, "groups": [], "summary": {},
                        "error": "nothing stored for that account yet"}
            with _read_only(path) as conn:
                return {
                    "key": key,
                    "groups": clip_groups(conn, assets=state.assets),
                    "summary": account_summary(conn),
                }

    return Handler


def free_port(preferred: int, host: str = "127.0.0.1") -> int:
    """`preferred` if nothing answers on it, otherwise whatever the OS hands out.

    A stale server from a previous run holding the port should not be the
    reason the UI does not come up.

    The question is asked by connecting, not by binding, and that distinction
    is the whole function. A trial bind reports the opposite answer on each
    platform: on Windows SO_REUSEADDR permits binding a port another process
    is actively listening on, so a busy port looks free and the two servers
    then split the incoming connections between them; on Linux, binding
    without it fails while a port is merely in TIME_WAIT, so the port we just
    released looks busy and the URL changes on every restart. A refused
    connection means nobody is listening, on both.
    """
    with socket.socket() as probe:
        probe.settimeout(0.3)
        if probe.connect_ex((host, preferred)) != 0:
            return preferred
    with socket.socket() as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def serve(
    cfg: Config,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    say: Callable[[str], None] = print,
) -> None:
    """Run the UI until interrupted.

    `host` defaults to loopback, which is the only configuration that needs no
    password: nothing outside the machine can reach it. Bind anything wider -
    a container binds 0.0.0.0 by necessity - and the gate matters, so set
    CLIPS_PASSWORD there. This prints a warning when it is missing rather than
    refusing, because "wider than loopback" is also a LAN address someone
    typed on purpose to open the page on their phone.
    """
    state = State(cfg=cfg)
    port = free_port(port, host)
    server = ThreadingHTTPServer((host, port), make_handler(state))
    server.daemon_threads = True
    # Twitch's player refuses to embed unless `parent` matches the page's own
    # hostname, and it does not accept a bare IP. The page builds that from
    # location.hostname, so the URL offered here has to be the name, not the
    # address it resolves to.
    url = f"http://localhost:{port}/"
    if host in ("127.0.0.1", "localhost", "::1"):
        say(f"clipsvalorant UI on {url}")
    else:
        say(f"clipsvalorant UI listening on {host}:{port}")
    say(f"  database: {cfg.db_path}")
    say(f"  account:  {cfg.riot_id} ({cfg.region.upper()})")
    if not cfg.has_twitch:
        say("  ! Twitch credentials missing - searching will stop after ingest.")
    if os.environ.get(PASSWORD_ENV, "").strip():
        say("  password: required (any username)")
    elif host not in ("127.0.0.1", "localhost", "::1"):
        say(f"  ! Bound to {host} with no password. Anyone who can reach this")
        say(f"  ! machine can spend its API quota. Set {PASSWORD_ENV} to gate it.")
    say("Press Ctrl+C to stop.")
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("\nStopped.")
    finally:
        server.shutdown()
        server.server_close()
        state.close()


# ---------------------------------------------------------------- the page

PAGE = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>val:grind</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta name="theme-color" content="#050505">
<style>
/* ===========================================================================
   Design read off github.com/wundercorp/awesome-components. That catalogue
   ships rendered DOM snapshots rather than source, so what is taken is the
   measured thing - spacing, radii, border alphas, type scale, motion timing -
   reimplemented here in plain CSS.

     shell, dot-grid, rules, card system   reapollo / bento-monochrome
     metric strip                          reapollo / glassmorphism-minimal-metrics-block
     theme control                         ncdai   / theme-switcher-1
     segmented controls                    ruixenui/ segmented-button-group
     scrubber ticks + round jumps          ruixenui/ audio-timeline-with-chapters
     search field                          kokonutd/ action-search-bar
     scan progress                         educalvolpz / animated-progress-bar
     match cards                           reapollo / bento-monochrome (spans)

   The background shader is written here - the catalogue has no shader source
   anywhere in it - and follows the monochrome silk of reuno-ui's
   background-paper-shaders.
   =========================================================================== */

:root{
  color-scheme:light;
  --bg:#ffffff; --ink:#0a0a0a;
  --muted:rgba(10,10,10,.58); --faint:rgba(10,10,10,.36);
  --line:rgba(10,10,10,.10); --hair:rgba(10,10,10,.06);
  --panel:rgba(255,255,255,.72); --solid:#ffffff;
  --grid:rgba(17,17,17,.075); --edge:#ffffff;
  --accent:#6b2fd4; --on-accent:#ffffff;
  --win:#1f7a4d; --loss:#b3331f;
  --shadow:0 10px 40px rgba(0,0,0,.045); --shadow-h:0 20px 60px rgba(0,0,0,.085);
  --shader:.5; --mapinvert:0;
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --bg:#050505; --ink:#fafafa;
  --muted:rgba(255,255,255,.58); --faint:rgba(255,255,255,.36);
  --line:rgba(255,255,255,.11); --hair:rgba(255,255,255,.06);
  --panel:rgba(255,255,255,.045); --solid:#0d0d0d;
  --grid:rgba(255,255,255,.06); --edge:#000000;
  --accent:#a97bff; --on-accent:#0a0a0a;
  --win:#43c98a; --loss:#ff6f57;
  --shadow:0 18px 40px rgba(0,0,0,.35); --shadow-h:0 28px 70px rgba(0,0,0,.55);
  --shader:.72; --mapinvert:0;
}

*,*::before,*::after{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0; background:var(--bg); color:var(--ink);
  font:400 15px/1.5 ui-sans-serif,-apple-system,"Segoe UI Variable Text","Segoe UI",Inter,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased; font-variant-numeric:tabular-nums; overflow-x:hidden;
}
.mono{font-family:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,monospace;
  font-variant-numeric:tabular-nums}

/* ---- background ---- */
#shader{position:fixed;inset:0;width:100%;height:100%;z-index:-30;opacity:var(--shader);
  transition:opacity .5s ease;display:block}
.dotgrid{position:fixed;inset:0;z-index:-20;pointer-events:none;opacity:.9;
  background-image:linear-gradient(to right,var(--grid) 1px,transparent 1px),
                   linear-gradient(to bottom,var(--grid) 1px,transparent 1px);
  background-size:20px 20px;
  -webkit-mask-image:repeating-linear-gradient(to right,#000 0 3px,transparent 3px 8px),
        repeating-linear-gradient(#000 0 3px,transparent 3px 8px);
  mask-image:repeating-linear-gradient(to right,#000 0 3px,transparent 3px 8px),
        repeating-linear-gradient(#000 0 3px,transparent 3px 8px);
  -webkit-mask-composite:source-in;mask-composite:intersect}
.vignette{position:fixed;inset:0;z-index:-10;pointer-events:none;opacity:.75;filter:blur(40px);
  background:radial-gradient(circle at center,rgba(0,0,0,0) 55%,var(--edge) 100%)}

/* ---- shell ---- */
.wrap{max-width:1152px;margin:0 auto;padding:48px 24px 72px;position:relative}
.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.35em;color:var(--faint);
  display:block;line-height:1}
.lbl{font-size:10px;text-transform:uppercase;letter-spacing:.28em;color:var(--faint);line-height:1}

.top{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;
  padding-bottom:20px;border-bottom:1px solid var(--line);margin-bottom:22px}
.brand h1{margin:10px 0 0;font-size:38px;line-height:1.02;letter-spacing:-.03em;font-weight:800}
.brand h1 .tag{color:var(--faint);font-weight:500}
.topright{display:flex;align-items:center;gap:10px}
.chip{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 12px;border-radius:999px;
  border:1px solid var(--line);color:var(--muted);font-size:10px;text-transform:uppercase;
  letter-spacing:.28em;background:var(--panel);backdrop-filter:blur(12px)}
/* A class selector beats the browser's own [hidden] rule, so a hidden chip
   stays a visible empty pill without this. */
.chip[hidden]{display:none}

/* theme: ncdai/theme-switcher-1 */
.themer{display:inline-flex;align-items:center;border-radius:999px;background:var(--panel);
  box-shadow:inset 0 0 0 1px var(--line);backdrop-filter:blur(12px);overflow:hidden}
.themer button{position:relative;width:32px;height:32px;display:grid;place-items:center;border:0;
  background:none;color:var(--faint);cursor:pointer;transition:color .2s}
.themer button:hover,.themer button[aria-checked="true"]{color:var(--ink)}
.themer button[aria-checked="true"]::after{content:"";position:absolute;inset:0;border-radius:999px;
  border:1px solid var(--line)}
.themer svg{width:15px;height:15px}

/* ---- search: kokonutd/action-search-bar ---- */
.scanrow{display:grid;grid-template-columns:1fr auto auto;gap:12px;align-items:end}
.field{display:flex;flex-direction:column;gap:8px;min-width:0}
.inputwrap{position:relative}
.inputwrap input{width:100%;height:44px;padding:0 40px 0 14px;border-radius:12px;
  border:1px solid var(--line);background:var(--panel);color:var(--ink);font-size:15px;
  backdrop-filter:blur(12px);outline:none;transition:border-color .2s,box-shadow .2s}
.inputwrap input::placeholder{color:var(--faint)}
.inputwrap input:focus{border-color:color-mix(in srgb,var(--accent) 55%,transparent);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 16%,transparent)}
.inputwrap svg{position:absolute;right:13px;top:50%;transform:translateY(-50%);
  width:16px;height:16px;color:var(--faint);pointer-events:none}

/* ---- segmented: ruixenui/segmented-button-group ---- */
.seg{display:inline-flex;height:44px;border-radius:999px;background:var(--panel);
  box-shadow:inset 0 0 0 1px var(--line);backdrop-filter:blur(12px);padding:4px;gap:2px}
.seg button{border:0;background:none;color:var(--muted);cursor:pointer;font:inherit;font-size:13px;
  padding:0 16px;border-radius:999px;transition:background .2s,color .2s;white-space:nowrap}
.seg button:hover{color:var(--ink)}
.seg button[aria-checked="true"],.seg button[aria-selected="true"]{background:var(--ink);color:var(--bg)}
.seg.sm{height:30px;padding:3px}
.seg.sm button{font-size:11px;padding:0 11px;text-transform:uppercase;letter-spacing:.1em}

.go{height:44px;padding:0 22px;border-radius:999px;border:0;background:var(--accent);
  color:var(--on-accent);font:inherit;font-weight:600;font-size:14px;cursor:pointer;
  display:inline-flex;align-items:center;gap:8px;transition:transform .2s,filter .2s}
.go:hover:not(:disabled){filter:brightness(1.08);transform:translateY(-1px)}
.go:disabled{opacity:.5;cursor:default}
.actions{display:flex;gap:8px;align-items:end}
.go.stop{background:none;color:var(--ink);box-shadow:inset 0 0 0 1px var(--line)}
.go.stop:hover:not(:disabled){background:var(--hair);filter:none}
.go.stop[hidden]{display:none}
.go svg{width:15px;height:15px}

/* ---- progress: educalvolpz/animated-progress-bar ---- */
.prog{margin-top:14px}
.prog[hidden]{display:none}
.pmeta{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;
  font-size:11px;text-transform:uppercase;letter-spacing:.24em;color:var(--muted)}
.ptrack{position:relative;height:6px;border-radius:999px;overflow:hidden;background:var(--hair);
  box-shadow:inset 0 0 0 1px var(--line)}
.pfill{height:100%;width:0;border-radius:999px;background:var(--ink);
  transition:width .45s cubic-bezier(.22,1,.36,1)}
.pfill.indet{width:35%;animation:slide 1.4s ease-in-out infinite}
@keyframes slide{0%{transform:translateX(-110%)}100%{transform:translateX(330%)}}
.notes{display:grid;gap:6px;margin-top:10px}
.notes:empty{display:none}
.notes div{font-size:12.5px;color:var(--ink);padding:8px 11px;border-radius:10px;
  border:1px solid var(--line);background:var(--panel);line-height:1.45}
.notes div::before{content:"";display:inline-block;width:5px;height:5px;border-radius:50%;
  background:var(--accent);margin-right:8px;vertical-align:middle}
.logbtn{margin-top:10px;border:0;background:none;color:var(--faint);font:inherit;font-size:11px;
  text-transform:uppercase;letter-spacing:.24em;cursor:pointer;padding:0}
.logbtn:hover{color:var(--ink)}
.log{margin:10px 0 0;max-height:180px;overflow:auto;font-family:ui-monospace,"Cascadia Mono",Menlo,Consolas,monospace;
  font-size:11.5px;line-height:1.7;color:var(--muted);white-space:pre-wrap;
  border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:var(--panel)}
.err{margin-top:12px;border:1px solid color-mix(in srgb,#e5484d 45%,transparent);border-radius:12px;
  padding:11px 14px;font-size:13px;color:#e5484d;background:color-mix(in srgb,#e5484d 7%,transparent)}
.err[hidden]{display:none}

/* ---- the quiet line of totals (was four big tiles) ---- */
.totals{display:flex;flex-wrap:wrap;gap:0 28px;align-items:baseline;margin:20px 0 6px;
  padding-bottom:14px;border-bottom:1px solid var(--hair)}
.totals span{font-size:11px;text-transform:uppercase;letter-spacing:.22em;color:var(--faint)}
.totals b{font-weight:650;color:var(--ink);font-size:13px;letter-spacing:0;margin-right:6px}

/* ---- cards ---- */
.card{position:relative;border:1px solid var(--line);border-radius:16px;background:var(--panel);
  backdrop-filter:blur(16px);box-shadow:var(--shadow)}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}
.rise{opacity:0;animation:rise .7s cubic-bezier(.22,1,.36,1) forwards}
@keyframes rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
.head{display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:12px 16px;border-bottom:1px solid var(--hair);min-height:52px}
.head .t{font-size:11px;text-transform:uppercase;letter-spacing:.28em;color:var(--faint)}

/* =========================== BRAND =======================================
   The mark is a frame with two corners drawn and three squares inside it:
   a moment bracketed out of something longer, which is the whole product.
   Rebuilt here as inline SVG from the brand sheet rather than linked as a
   file, because one <symbol> theming itself off currentColor survives the
   light/dark switch that a PNG would not.

   The wordmark's colon is two squares rather than a typed ':' - the glyph
   is round in most sans faces, and the squares are the mark's own shape
   repeated. Sized in em, so it scales with whatever it sits in.
   ========================================================================= */
/* `vgmark`, not `mark`: the scrubber's ticks took that name first, and
   theirs is position:absolute - a brand mark inheriting it leaves the
   logo pinned to the top-left corner of the viewport. */
.vgmark{display:block;color:var(--ink)}
.vgmark .br{stroke:currentColor;fill:none;stroke-linecap:square}
.vgmark .sq{fill:var(--accent)}
.lockup{display:flex;align-items:center;gap:11px;text-decoration:none}
.lockup .vgmark{width:26px;height:26px}
.lockup .br{stroke-width:2.3}
.lockup .word{font-size:20px}
.word{font-weight:800;letter-spacing:-.035em;line-height:1;color:var(--ink);white-space:nowrap}
.word .cl{display:inline-block;position:relative;width:.19em;margin:0 .13em;
  vertical-align:baseline}
.word .cl::before,.word .cl::after{content:"";position:absolute;left:0;
  width:.19em;height:.19em;background:var(--accent)}
.word .cl::before{bottom:.44em}
.word .cl::after{bottom:.02em}

/* =========================== SPLASH ======================================
   Once per browser session, not once per page view: a brand animation you
   cannot get past is charming the first time and an obstacle every time
   after. Click, key or Escape cuts it short, and reduced-motion skips it.
   ========================================================================= */
#splash{position:fixed;inset:0;z-index:140;display:grid;place-items:center;
  background:var(--bg);transition:opacity .45s ease,visibility .45s}
#splash[hidden]{display:none}
#splash.gone{opacity:0;visibility:hidden;pointer-events:none}
.sp{display:grid;justify-items:center;gap:20px;padding:24px;text-align:center}
.sp .vgmark{width:96px;height:96px}
/* pathLength="1" is what makes one dasharray work for both corners whatever
   the viewBox - the browser normalises the length for us. */
.sp .br{stroke-width:2.1;stroke-dasharray:1;stroke-dashoffset:1;
  animation:vgdraw .6s cubic-bezier(.22,1,.36,1) forwards}
.sp .br.b2{animation-delay:.12s}
.sp .sq{opacity:0;transform-origin:center;
  animation:vgpop .38s cubic-bezier(.34,1.56,.64,1) forwards}
.sp .s1{animation-delay:.46s} .sp .s2{animation-delay:.56s} .sp .s3{animation-delay:.66s}
.sp .word{font-size:clamp(34px,8vw,52px);opacity:0;animation:vgrise .6s .74s cubic-bezier(.22,1,.36,1) forwards}
.sp .tagline{font-size:clamp(15px,3.4vw,19px);font-weight:600;color:var(--muted);
  opacity:0;animation:vgrise .6s .92s cubic-bezier(.22,1,.36,1) forwards}
.sp .tc{display:flex;align-items:center;gap:14px;opacity:0;
  animation:vgrise .6s 1.04s cubic-bezier(.22,1,.36,1) forwards}
.sp .tc span{font-size:11px;letter-spacing:.22em;color:var(--faint)}
.sp .tc .bar{width:clamp(80px,22vw,150px);height:1px;background:var(--line);position:relative}
.sp .tc .bar::after{content:"";position:absolute;inset:0 100% 0 0;background:var(--accent);
  animation:vgwipe 1.1s .2s cubic-bezier(.22,1,.36,1) forwards}
@keyframes vgdraw{to{stroke-dashoffset:0}}
@keyframes vgpop{0%{opacity:0;transform:scale(.25)}100%{opacity:1;transform:scale(1)}}
@keyframes vgrise{0%{opacity:0;transform:translateY(12px)}100%{opacity:1;transform:none}}
@keyframes vgwipe{to{inset:0 0 0 0}}

/* ============================ THE QUEUE ==================================
   Shown only while your own scan is waiting. Somebody else's scan never
   takes the page away from you - the clips already found are readable the
   whole time, which is why there is a way out of this screen.
   ========================================================================= */
#gate{position:fixed;inset:0;z-index:130;display:grid;place-items:center;padding:24px;
  background:color-mix(in srgb,var(--bg) 86%,transparent);backdrop-filter:blur(18px);
  animation:vgfade .45s ease}
#gate[hidden]{display:none}
#gate.gone{opacity:0;pointer-events:none;transition:opacity .4s ease}
@keyframes vgfade{0%{opacity:0}100%{opacity:1}}
.gt{display:grid;justify-items:center;gap:22px;text-align:center;max-width:460px;width:100%}
.gt .vgmark{width:64px;height:64px}
.gt .br{stroke-width:2.2;animation:vgbreathe 2.6s ease-in-out infinite}
/* The idle animation: the three squares take turns, in the order they are
   stacked, at the pace of something working rather than something stuck. */
.gt .sq{animation:vgblip 1.9s ease-in-out infinite}
.gt .s2{animation-delay:.22s} .gt .s3{animation-delay:.44s}
@keyframes vgblip{0%,68%,100%{opacity:.22}18%,42%{opacity:1}}
@keyframes vgbreathe{0%,100%{opacity:.45}50%{opacity:1}}
.gt .eyebrow{letter-spacing:.32em}
.gt .pos{font-size:clamp(64px,18vw,92px);font-weight:800;letter-spacing:-.045em;line-height:.88}
.gt .posnote{font-size:14px;color:var(--muted);margin-top:-8px}
.gt .now{font-size:12.5px;color:var(--faint);line-height:1.6}
.gt .now b{color:var(--ink);font-weight:600}
/* The timeline motif off the brand sheet: ticks, and a marker that moves. */
.qrail{position:relative;height:1px;background:var(--line);width:100%;max-width:380px;
  margin:6px 0 2px}
.qrail i{position:absolute;top:-3px;width:1px;height:7px;background:var(--line)}
.qrail .me{top:-5px;width:2px;height:11px;background:var(--accent);
  transition:left .7s cubic-bezier(.22,1,.36,1)}
.qends{display:flex;justify-content:space-between;width:100%;max-width:380px;
  font-size:10.5px;letter-spacing:.2em;color:var(--faint)}
.gtbtns{display:flex;gap:10px;flex-wrap:wrap;justify-content:center}
.ghost{height:40px;padding:0 16px;border-radius:999px;border:1px solid var(--line);
  background:var(--panel);color:var(--muted);font:inherit;font-size:13px;cursor:pointer;
  backdrop-filter:blur(12px);transition:color .2s,border-color .2s}
.ghost:hover{color:var(--ink);border-color:color-mix(in srgb,var(--ink) 25%,transparent)}
.ghost:disabled{opacity:.5;cursor:default}

/* The compact version, for when you have stepped out of the queue screen. */
.qback{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:60;
  display:flex;align-items:center;gap:12px;height:44px;padding:0 8px 0 16px;
  border-radius:999px;border:1px solid var(--line);background:var(--solid);
  box-shadow:var(--shadow-h);font-size:13px}
.qback[hidden]{display:none}
.qback b{font-weight:700}
.qback button{height:30px;padding:0 13px;border-radius:999px;border:0;cursor:pointer;
  background:var(--ink);color:var(--bg);font:inherit;font-size:12px;font-weight:600}

@media (prefers-reduced-motion:reduce){
  .sp .br,.sp .sq,.sp .word,.sp .tagline,.sp .tc,.sp .tc .bar::after,
  .gt .sq,.gt .br{animation:none!important;opacity:1;stroke-dashoffset:0;transform:none}
  #gate{animation:none}
}

/* ================= THE MATCH PICKER - the hero of the page ================ */
.pick{margin:18px 0 6px}
.pickhead{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:12px}
.pickhead h2{margin:0;font-size:22px;letter-spacing:-.02em;font-weight:700}
.matches{display:grid;grid-template-columns:repeat(auto-fill,minmax(336px,1fr));gap:12px}
.match{position:relative;overflow:hidden;text-align:left;cursor:pointer;color:inherit;font:inherit;
  border:1px solid var(--line);border-radius:16px;background:var(--panel);backdrop-filter:blur(16px);
  padding:0;box-shadow:var(--shadow);transition:transform .25s cubic-bezier(.22,1,.36,1),
  box-shadow .25s ease,border-color .25s ease}
.match:hover{transform:translateY(-3px);box-shadow:var(--shadow-h)}
.match[aria-current="true"]{border-color:var(--accent);
  box-shadow:var(--shadow-h),0 0 0 1px var(--accent) inset}
/* the map's own splash, kept far back so it reads as texture not decoration */
.match .splash{position:absolute;inset:0;background-size:cover;background-position:center;
  opacity:.14;filter:grayscale(1) contrast(1.1);transition:opacity .3s ease}
.match:hover .splash,.match[aria-current="true"] .splash{opacity:.26}
.match .veil{position:absolute;inset:0;background:
  linear-gradient(90deg,var(--solid) 18%,color-mix(in srgb,var(--solid) 55%,transparent) 70%,
  color-mix(in srgb,var(--solid) 15%,transparent))}
.match .inner{position:relative;padding:14px 16px;display:grid;gap:11px}
.mrow1{display:flex;align-items:center;gap:10px}
.av{width:36px;height:36px;border-radius:50%;flex:none;object-fit:cover;
  border:1px solid var(--line);background:var(--hair)}
.av.ph{display:grid;place-items:center;font-size:13px;font-weight:700;color:var(--faint)}
.mwho{min-width:0;flex:1}
.mwho .ch{display:block;font-size:16px;font-weight:700;letter-spacing:-.015em;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mwho .sub{display:block;font-size:11px;color:var(--muted);margin-top:2px}
.badge{flex:none;font-size:9px;text-transform:uppercase;letter-spacing:.18em;padding:3px 7px;
  border-radius:999px;border:1px solid var(--line);color:var(--muted);line-height:1}
.badge.confirmed{border-color:color-mix(in srgb,var(--accent) 55%,transparent);color:var(--accent)}

.mrow2{display:flex;align-items:center;gap:9px}
.agent{width:30px;height:30px;border-radius:9px;flex:none;object-fit:cover;
  border:1px solid var(--line);background:var(--hair)}
.mscore{font-size:19px;font-weight:750;letter-spacing:-.02em;line-height:1}
.mscore .w{color:var(--win)} .mscore .l{color:var(--loss)}
.mmap{font-size:12px;color:var(--muted)}
.mkda{margin-left:auto;text-align:right;font-size:12px;color:var(--muted);line-height:1.35}
.mkda b{color:var(--ink);font-weight:650}

.mrow3{display:flex;align-items:center;gap:8px;padding-top:10px;border-top:1px solid var(--hair)}
.pips{display:flex;gap:2px;flex:1;min-width:0;flex-wrap:nowrap;overflow:hidden}
.pip{height:12px;flex:1 1 0;min-width:2px;border-radius:1px;background:var(--line)}
.pip.w{background:var(--win)} .pip.l{background:var(--loss)}
.mcount{flex:none;font-size:11px;color:var(--faint);letter-spacing:.06em}
.mcount b{color:var(--ink);font-weight:650;font-size:12.5px}

/* ======================= the opened match ================================ */
.stageWrap{margin-top:12px}
.left{grid-column:span 7;display:flex;flex-direction:column;overflow:hidden}
.right{grid-column:span 5;display:flex;flex-direction:column;gap:12px;min-width:0}
.player{position:relative;aspect-ratio:16/9;background:#000;border-bottom:1px solid var(--hair)}
.player iframe,.player>div{position:absolute;inset:0;width:100%;height:100%;border:0}
.deck{padding:12px 16px 16px}
.nowline{display:flex;align-items:baseline;gap:10px;margin-bottom:10px;min-height:20px}
.nowline .n{font-size:13px;font-weight:650;letter-spacing:-.01em}
.nowline .w{font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.2em}
.nowline .ts{margin-left:auto;font-size:12px;color:var(--muted)}

/* scrubber: ruixenui/audio-timeline-with-chapters */
.scrub{position:relative;height:30px;cursor:pointer;touch-action:none}
.rail{position:absolute;left:0;right:0;top:13px;height:4px;border-radius:999px;background:var(--hair);
  box-shadow:inset 0 0 0 1px var(--line)}
.mark{position:absolute;top:0;transform:translateX(-50%);border:0;padding:0;background:none;cursor:pointer}
.mark i{display:block;border-radius:1px;background:var(--faint);transition:background .15s,transform .15s}
.mark.round i{width:1px;height:12px;margin-top:9px;background:var(--line)}
.mark.kill i{width:2px;height:16px;margin-top:7px;background:var(--muted)}
.mark.star i{width:3px;height:24px;margin-top:3px;border-radius:2px;background:var(--accent)}
.mark:hover i,.mark:focus-visible i{background:var(--ink);transform:scaleY(1.22)}
.mark.star:hover i{background:var(--accent)}
.playhead{position:absolute;top:2px;width:2px;height:26px;border-radius:2px;background:var(--accent);
  transform:translateX(-50%);pointer-events:none;opacity:0;transition:opacity .25s}
.playhead.on{opacity:1}
.tip{position:absolute;bottom:34px;transform:translateX(-50%);white-space:nowrap;pointer-events:none;
  background:var(--solid);color:var(--ink);border:1px solid var(--line);border-radius:8px;
  padding:5px 9px;font-size:11.5px;box-shadow:var(--shadow-h);opacity:0;transition:opacity .15s;z-index:6}
.tip.on{opacity:1}
.tip b{font-weight:650} .tip span{color:var(--faint);margin-left:6px}
.axis{display:flex;justify-content:space-between;margin-top:3px;font-size:10.5px;color:var(--faint)}

/* ---- round timeline strip ---- */
.rounds{display:flex;flex-wrap:wrap;gap:3px;margin-top:14px;padding-top:13px;
  border-top:1px solid var(--hair)}
.rnd{position:relative;width:30px;height:34px;border-radius:7px;border:1px solid var(--line);
  background:none;color:var(--muted);font:inherit;font-size:11px;cursor:pointer;padding:0;
  display:grid;place-items:center;transition:background .16s,color .16s,border-color .16s}
.rnd:hover{background:var(--hair);color:var(--ink)}
.rnd[aria-pressed="true"]{border-color:var(--accent);color:var(--ink);
  box-shadow:0 0 0 1px var(--accent) inset}
.rnd .bar{position:absolute;left:5px;right:5px;bottom:4px;height:2px;border-radius:2px;
  background:var(--line)}
.rnd.w .bar{background:var(--win)} .rnd.l .bar{background:var(--loss)}
.rnd .n{margin-bottom:5px;font-variant-numeric:tabular-nums}
.rnd .spike{position:absolute;top:3px;right:4px;width:3px;height:3px;border-radius:50%;
  background:var(--accent);opacity:.9}
.rnd:disabled{opacity:.4;cursor:default}
.rndnote{margin-top:9px;font-size:11px;color:var(--faint)}

/* ---- the map ---- */
.mapwrap{position:relative;width:100%;aspect-ratio:1/1;border-radius:12px;overflow:hidden;
  background:var(--hair);border:1px solid var(--hair)}
/* Riot's minimap is white geometry on transparent, so it is inverted in the
   light theme and held well back in both: the dots are the subject here and
   the map is the paper they sit on. */
.mapimg{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;
  opacity:.4;filter:grayscale(1) contrast(1.05)}
:root[data-theme="light"] .mapimg{opacity:.5;filter:grayscale(1) contrast(1.05) invert(1)}
.dot{position:absolute;transform:translate(-50%,-50%);border:0;padding:0;cursor:pointer;
  border-radius:50%;transition:transform .15s ease,opacity .15s ease}
.dot.other{width:6px;height:6px;background:var(--faint);opacity:.5}
.dot.mine{width:9px;height:9px;background:var(--accent);
  box-shadow:0 0 0 2px color-mix(in srgb,var(--solid) 70%,transparent)}
.dot.death{width:9px;height:9px;background:var(--loss);
  box-shadow:0 0 0 2px color-mix(in srgb,var(--solid) 70%,transparent)}
.dot:hover{transform:translate(-50%,-50%) scale(1.5);opacity:1;z-index:3}
.dot.dim{opacity:.08;pointer-events:none}
/* With a round picked there are only a handful left, so they stop being a
   scatter and become the subject: full strength and a size up. */
.mapwrap.filtered .dot:not(.dim){opacity:1;transform:translate(-50%,-50%) scale(1.3)}
.mapwrap.filtered .dot.other{background:var(--muted)}
.maplegend{display:flex;gap:14px;align-items:center;padding:10px 16px 0;flex-wrap:wrap}
.key{display:inline-flex;align-items:center;gap:6px;font-size:11px;color:var(--muted)}
.key i{width:8px;height:8px;border-radius:50%;display:block}
.key.mine i{background:var(--accent)} .key.death i{background:var(--loss)}
.key.other i{background:var(--faint);opacity:.55}
.nomap{display:grid;place-items:center;height:100%;text-align:center;padding:28px;color:var(--faint)}
.nomap p{margin:8px 0 0;font-size:12px;color:var(--muted);max-width:30ch}

/* ---- scoreboard ---- */
.board{width:100%;border-collapse:collapse;font-size:12.5px}
.board th{text-align:right;font-size:9.5px;text-transform:uppercase;letter-spacing:.18em;
  color:var(--faint);font-weight:500;padding:0 0 8px}
.board th:first-child,.board td:first-child{text-align:left}
.board td{padding:5px 0;border-top:1px solid var(--hair);text-align:right;color:var(--muted)}
.board tr.me td{color:var(--ink);font-weight:600}
.board tr.me td:first-child{box-shadow:inset 2px 0 0 var(--accent);padding-left:7px}
.board tr.foe td:first-child{color:var(--muted)}
.who{display:flex;align-items:center;gap:7px;min-width:0}
.who img{width:20px;height:20px;border-radius:6px;flex:none;background:var(--hair)}
.who span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sep td{padding:0;border-top:1px solid var(--line);height:7px}

.moments{list-style:none;margin:13px 0 0;padding:13px 0 0;border-top:1px solid var(--hair);
  display:grid;gap:2px;max-height:168px;overflow:auto}
.moment{display:flex;align-items:center;gap:10px;width:100%;border:0;background:none;color:inherit;
  font:inherit;text-align:left;padding:6px 8px;border-radius:9px;cursor:pointer;transition:background .16s}
.moment:hover{background:var(--hair)}
.moment .lab{font-size:13px;font-weight:550;letter-spacing:-.01em}
.moment .sub{font-size:11.5px;color:var(--faint)}
.moment .st{margin-left:auto;font-size:11.5px;color:var(--muted)}
.star-ico{width:11px;height:11px;flex:none;color:var(--accent)}

.note{padding:11px 16px;font-size:11.5px;color:var(--muted);border-top:1px solid var(--hair);
  line-height:1.6}
.empty{padding:48px 24px;text-align:center;color:var(--faint)}
.empty p{margin:10px 0 0;font-size:13px;max-width:36ch;margin-inline:auto;color:var(--muted)}
.foot{margin-top:26px;padding-top:18px;border-top:1px solid var(--line);
  font-size:10px;text-transform:uppercase;letter-spacing:.3em;color:var(--faint);
  display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}

::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:999px}
::-webkit-scrollbar-track{background:transparent}

@media (max-width:980px){
  .left,.right{grid-column:span 12}
}
@media (max-width:900px){
  .wrap{padding:32px 18px 56px}
  .brand h1{font-size:28px}
  .scanrow{grid-template-columns:1fr}
  .matches{grid-template-columns:1fr}
}
@media (prefers-reduced-motion:reduce){
  *{animation-duration:.001ms !important;transition-duration:.001ms !important}
  .rise{opacity:1}
}
::view-transition-old(root),::view-transition-new(root){animation:none;mix-blend-mode:normal}
::view-transition-old(root){z-index:0} ::view-transition-new(root){z-index:1}
</style>
</head>
<body>
<canvas id="shader" aria-hidden="true"></canvas>
<div class="dotgrid" aria-hidden="true"></div>
<div class="vignette" aria-hidden="true"></div>

<div id="splash" hidden>
  <div class="sp">
    <svg class="vgmark" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path class="br b1" pathLength="1" d="M3.2 9.4V3.2h6.2"/>
      <path class="br b2" pathLength="1" d="M20.8 14.6v6.2h-6.2"/>
      <rect class="sq s1" x="13.1" y="6.9" width="3.4" height="3.4"/>
      <rect class="sq s2" x="8.3" y="10.3" width="3.4" height="3.4"/>
      <rect class="sq s3" x="13.1" y="13.7" width="3.4" height="3.4"/>
    </svg>
    <div class="word">val<span class="cl"></span>grind</div>
    <div class="tagline">Your play. Their POV.</div>
    <div class="tc"><span class="mono">00:00:00</span><span class="bar"></span>
      <span class="mono" id="sptc">00:24:08</span></div>
  </div>
</div>

<div id="gate" hidden>
  <div class="gt">
    <svg class="vgmark" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path class="br b1" pathLength="1" d="M3.2 9.4V3.2h6.2"/>
      <path class="br b2" pathLength="1" d="M20.8 14.6v6.2h-6.2"/>
      <rect class="sq s1" x="13.1" y="6.9" width="3.4" height="3.4"/>
      <rect class="sq s2" x="8.3" y="10.3" width="3.4" height="3.4"/>
      <rect class="sq s3" x="13.1" y="13.7" width="3.4" height="3.4"/>
    </svg>
    <span class="eyebrow">In the queue</span>
    <div>
      <div class="pos mono" id="gatepos">&mdash;</div>
      <div class="posnote" id="gatenote">Working out where you are</div>
    </div>
    <div class="qrail" id="qrail"><i class="me" id="qme" style="left:0"></i></div>
    <div class="qends"><span class="mono">NOW</span><span class="mono" id="qeta"></span></div>
    <div class="now" id="gatenow"></div>
    <div class="gtbtns">
      <button type="button" class="ghost" id="gatebrowse">Browse stored clips</button>
      <button type="button" class="ghost" id="gateleave">Leave the queue</button>
    </div>
  </div>
</div>

<div class="qback" id="qback" hidden>
  <span>Queued &middot; <b id="qbackpos">&mdash;</b></span>
  <button type="button" id="qbackopen">Show</button>
</div>

<main class="wrap">
  <header class="top">
    <div class="brand">
      <a class="lockup" href="/" aria-label="val:grind">
        <svg class="vgmark" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path class="br b1" pathLength="1" d="M3.2 9.4V3.2h6.2"/>
          <path class="br b2" pathLength="1" d="M20.8 14.6v6.2h-6.2"/>
          <rect class="sq s1" x="13.1" y="6.9" width="3.4" height="3.4"/>
          <rect class="sq s2" x="8.3" y="10.3" width="3.4" height="3.4"/>
          <rect class="sq s3" x="13.1" y="13.7" width="3.4" height="3.4"/>
        </svg>
        <span class="word">val<span class="cl"></span>grind</span>
      </a>
      <h1 id="who">&mdash;</h1>
    </div>
    <div class="topright">
      <span class="chip" id="qchip" hidden></span>
      <span class="chip" id="region" hidden></span>
      <div class="themer" role="radiogroup" aria-label="Colour theme">
        <button type="button" role="radio" data-theme="system" aria-checked="false" aria-label="Match the system theme" title="System">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>
        </button>
        <button type="button" role="radio" data-theme="light" aria-checked="false" aria-label="Light theme" title="Light">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
        </button>
        <button type="button" role="radio" data-theme="dark" aria-checked="false" aria-label="Dark theme" title="Dark">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
        </button>
      </div>
    </div>
  </header>

  <section class="scan">
    <form class="scanrow" id="scanform" autocomplete="off">
      <div class="field">
        <label class="lbl" for="riot">Riot ID</label>
        <div class="inputwrap">
          <input id="riot" name="riot" type="text" placeholder="name#tag" spellcheck="false">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        </div>
      </div>
      <div class="field">
        <span class="lbl">Window</span>
        <div class="seg" role="radiogroup" aria-label="How far back to scan" id="window">
          <button type="button" role="radio" data-days="7" aria-checked="false">7d</button>
          <button type="button" role="radio" data-days="14" aria-checked="true">14d</button>
          <button type="button" role="radio" data-days="30" aria-checked="false">30d</button>
        </div>
      </div>
      <div class="field">
        <span class="lbl">Queue</span>
        <div class="seg" role="radiogroup" aria-label="Which queue to scan" id="queue">
          <button type="button" role="radio" data-mode="competitive" aria-checked="true">Comp</button>
          <button type="button" role="radio" data-mode="any" aria-checked="false">All</button>
        </div>
      </div>
      <div class="actions">
        <button class="go" id="go" type="submit">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
          Scan
        </button>
        <button class="go stop" id="stop" type="button" hidden>
          <svg viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="7" width="10" height="10" rx="2"/></svg>
          Stop
        </button>
      </div>
    </form>

    <div class="prog" id="prog" hidden>
      <div class="pmeta"><span id="pphase">Starting</span><span id="ppct" class="mono"></span></div>
      <div class="ptrack"><div class="pfill" id="pfill"></div></div>
      <div class="notes" id="notes"></div>
      <button class="logbtn" id="logbtn" type="button" aria-expanded="false">Show log</button>
      <pre class="log" id="log" hidden></pre>
    </div>
    <div class="err" id="err" hidden></div>
  </section>

  <div class="totals" id="totals"></div>

  <section class="pick">
    <div class="pickhead">
      <h2 id="pickTitle">Your matches on someone's stream</h2>
      <div class="seg sm" role="tablist" aria-label="Filter matches" id="filter">
        <button type="button" role="tab" data-f="all" aria-selected="true">All</button>
        <button type="button" role="tab" data-f="confirmed" aria-selected="false">Confirmed</button>
      </div>
    </div>
    <div class="matches" id="matches"></div>
  </section>

  <section class="grid stageWrap" id="stageWrap"></section>

  <footer class="foot">
    <span>Timestamps measured against each broadcast</span>
    <span id="footright"></span>
  </footer>
</main>

<script>
/* =====================================================================
   background shader - monochrome domain-warped flow.
   Nothing in awesome-components ships shader source, so this is written
   here; the look follows reuno-ui/background-paper-shaders.
   ===================================================================== */
(function shader(){
  var cv = document.getElementById("shader"), gl = null;
  try { gl = cv.getContext("webgl", {antialias:false, alpha:true, depth:false, powerPreference:"low-power"})
             || cv.getContext("experimental-webgl"); } catch (e) { gl = null; }
  if (!gl) { cv.style.display = "none"; return; }
  var VS = "attribute vec2 p;void main(){gl_Position=vec4(p,0.0,1.0);}";
  var FS = [
    "precision highp float;",
    "uniform vec2 u_res;uniform float u_t;uniform float u_dark;uniform vec2 u_m;",
    "float hash(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453123);}",
    "float noise(vec2 p){vec2 i=floor(p),f=fract(p);vec2 u=f*f*(3.0-2.0*f);",
    " return mix(mix(hash(i),hash(i+vec2(1.0,0.0)),u.x),",
    "            mix(hash(i+vec2(0.0,1.0)),hash(i+vec2(1.0,1.0)),u.x),u.y);}",
    "float fbm(vec2 p){float v=0.0,a=0.5;mat2 m=mat2(1.6,1.2,-1.2,1.6);",
    " for(int i=0;i<5;i++){v+=a*noise(p);p=m*p;a*=0.5;}return v;}",
    "void main(){",
    " vec2 uv=(gl_FragCoord.xy-0.5*u_res)/min(u_res.x,u_res.y);",
    " float t=u_t*0.035; vec2 par=u_m*0.07;",
    " vec2 q=vec2(fbm(uv*1.35+par+vec2(0.0,t)),fbm(uv*1.35+par+vec2(5.2,1.3-t)));",
    " vec2 r=vec2(fbm(uv*1.35+2.2*q+vec2(1.7,9.2)+0.15*t),",
    "             fbm(uv*1.35+2.2*q+vec2(8.3,2.8)-0.12*t));",
    " float f=fbm(uv*1.35+2.0*r);",
    " float v=smoothstep(0.22,0.96,f);",
    " vec3 dark=mix(vec3(0.015),vec3(0.33),v);",
    " vec3 lite=mix(vec3(1.0),vec3(0.66),v);",
    " vec3 col=mix(lite,dark,u_dark);",
    " col+=(hash(gl_FragCoord.xy+fract(u_t)*vec2(13.0,7.0))-0.5)*0.032;",
    " gl_FragColor=vec4(col,1.0);}"
  ].join("\n");
  function compile(type, src){
    var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    return gl.getShaderParameter(s, gl.COMPILE_STATUS) ? s : null;
  }
  var vs = compile(gl.VERTEX_SHADER, VS), fs = compile(gl.FRAGMENT_SHADER, FS);
  if (!vs || !fs) { cv.style.display = "none"; return; }
  var pr = gl.createProgram();
  gl.attachShader(pr, vs); gl.attachShader(pr, fs); gl.linkProgram(pr);
  if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) { cv.style.display = "none"; return; }
  gl.useProgram(pr);
  var buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,3,-1,-1,3]), gl.STATIC_DRAW);
  var loc = gl.getAttribLocation(pr, "p");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  var uRes = gl.getUniformLocation(pr, "u_res"), uT = gl.getUniformLocation(pr, "u_t"),
      uD = gl.getUniformLocation(pr, "u_dark"), uM = gl.getUniformLocation(pr, "u_m");
  var mx = 0, my = 0, tx = 0, ty = 0;
  addEventListener("pointermove", function(e){
    tx = (e.clientX / innerWidth - 0.5) * 2; ty = (0.5 - e.clientY / innerHeight) * 2;
  }, {passive:true});
  function size(){
    var dpr = Math.min(devicePixelRatio || 1, 1.25);
    var w = Math.max(1, Math.round(innerWidth * dpr)), h = Math.max(1, Math.round(innerHeight * dpr));
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; gl.viewport(0, 0, w, h); }
  }
  addEventListener("resize", size, {passive:true});
  size();
  var still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  var t0 = performance.now(), last = 0;
  function frame(now){
    requestAnimationFrame(frame);
    if (document.hidden || now - last < 33) { return; }
    last = now;
    mx += (tx - mx) * 0.045; my += (ty - my) * 0.045;
    size();
    gl.uniform2f(uRes, cv.width, cv.height);
    gl.uniform1f(uT, still ? 0 : (now - t0) / 1000);
    gl.uniform1f(uD, document.documentElement.dataset.theme === "dark" ? 1 : 0);
    gl.uniform2f(uM, mx, my);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
  requestAnimationFrame(frame);
})();

/* ===================================================================== */

var $  = function(s, r){ return (r || document).querySelector(s); };
var $$ = function(s, r){ return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
var esc = function(s){
  return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
};
var hhmmss = function(sec){
  sec = Math.max(0, Math.round(sec || 0));
  var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  var p = function(n){ return (n < 10 ? "0" : "") + n; };
  return h ? h + ":" + p(m) + ":" + p(s) : m + ":" + p(s);
};
var short = function(riot){ return String(riot || "").split("#")[0]; };
/* Riot numbers rounds from zero; people do not. Every round shown to a
   human goes through here so the strip, the tooltips, the map and the
   moment list can never disagree with each other. */
var rno = function(n){ return Number(n) + 1; };

var pad2 = function(n){ return (n < 10 ? "0" : "") + n; };

var S = {
  key: "", groups: [], sel: null, filter: "all", days: 14, mode: "competitive",
  round: null,
  player: null, poll: null, tick: null,
  /* the queue: your ticket, and whether you have stepped out of the
     waiting screen to look at clips while you wait */
  ticket: "", gateOut: false, qtick: null
};

/* ---- theme: ncdai/theme-switcher-1, with a circular View Transition ---- */
var media = matchMedia("(prefers-color-scheme: dark)");
function resolved(pref){ return pref === "system" ? (media.matches ? "dark" : "light") : pref; }
function paint(pref){
  document.documentElement.dataset.theme = resolved(pref);
  $$(".themer button").forEach(function(b){
    b.setAttribute("aria-checked", String(b.dataset.theme === pref));
  });
}
function setTheme(pref, ev){
  try { localStorage.setItem("cv-theme", pref); } catch (e) {}
  if (!document.startViewTransition || matchMedia("(prefers-reduced-motion: reduce)").matches) {
    paint(pref); return;
  }
  var x = ev ? ev.clientX : innerWidth - 60, y = ev ? ev.clientY : 50;
  var far = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
  document.startViewTransition(function(){ paint(pref); }).ready.then(function(){
    document.documentElement.animate(
      { clipPath: ["circle(0px at " + x + "px " + y + "px)",
                   "circle(" + far + "px at " + x + "px " + y + "px)"] },
      { duration: 480, easing: "cubic-bezier(.22,1,.36,1)",
        pseudoElement: "::view-transition-new(root)" });
  }).catch(function(){});
}
var pref = "system";
try { pref = localStorage.getItem("cv-theme") || "system"; } catch (e) {}
paint(pref);
media.addEventListener("change", function(){
  var p = "system";
  try { p = localStorage.getItem("cv-theme") || "system"; } catch (e) {}
  if (p === "system") { paint(p); }
});
$$(".themer button").forEach(function(b){
  b.addEventListener("click", function(ev){ setTheme(b.dataset.theme, ev); });
});

$$("#window button").forEach(function(b){
  b.addEventListener("click", function(){
    S.days = Number(b.dataset.days);
    $$("#window button").forEach(function(o){ o.setAttribute("aria-checked", String(o === b)); });
  });
});
$$("#queue button").forEach(function(b){
  b.addEventListener("click", function(){
    S.mode = b.dataset.mode;
    $$("#queue button").forEach(function(o){ o.setAttribute("aria-checked", String(o === b)); });
  });
});
$$("#filter button").forEach(function(b){
  b.addEventListener("click", function(){
    S.filter = b.dataset.f;
    $$("#filter button").forEach(function(o){ o.setAttribute("aria-selected", String(o === b)); });
    renderMatches();
  });
});

/* ---- totals, now one quiet line ---- */
function renderTotals(){
  var channels = {}, matches = {}, vods = {}, moments = 0;
  S.groups.forEach(function(g){
    channels[g.channel] = 1; matches[g.match_id] = 1;
    if (!vods[g.vod_id]) { vods[g.vod_id] = g.vod_duration || 0; }
    moments += g.playable || 0;
  });
  var hours = 0;
  Object.keys(vods).forEach(function(v){ hours += vods[v]; });
  $("#totals").innerHTML =
    '<span><b>' + Object.keys(matches).length + '</b>matches</span>' +
    '<span><b>' + Object.keys(channels).length + '</b>streamers</span>' +
    '<span><b>' + moments + '</b>playable moments</span>' +
    '<span><b>' + (Math.round(hours / 360) / 10) + 'h</b>of broadcast</span>';
}

/* ================= the match picker ================= */
function visible(){
  return S.filter === "all" ? S.groups
       : S.groups.filter(function(g){ return g.verdict === "confirmed"; });
}
function pips(g){
  var rs = (g.rounds || []);
  if (!rs.length) { return ""; }
  return '<span class="pips">' + rs.map(function(r){
    return '<i class="pip ' + (r.winner ? (r.won ? "w" : "l") : "") + '"></i>';
  }).join("") + '</span>';
}
function renderMatches(){
  var box = $("#matches"), gs = visible();
  if (!gs.length) {
    box.innerHTML = '<div class="card empty"><span class="eyebrow">Nothing yet</span><p>' +
      (S.groups.length ? "No confirmed matches in this account."
                       : "Run a scan to find lobby-mates who were live.") + '</p></div>';
    $("#stageWrap").innerHTML = "";
    return;
  }
  box.innerHTML = gs.map(function(g, i){
    var me = g.me || {};
    var won = g.won === true, lost = g.won === false;
    var parts = String(g.score || "").split("-");
    return '<button class="match rise" data-id="' + esc(g.id) + '"' +
      ' style="animation-delay:' + Math.min(i * 0.05, 0.4) + 's"' +
      ' aria-current="' + String(S.sel === g.id) + '">' +
      (g.map_id ? '<span class="splash" style="background-image:url(/api/icon/mapsplash/' +
                  esc(g.map_id) + '.png)"></span>' : '') +
      '<span class="veil"></span>' +
      '<span class="inner">' +
        '<span class="mrow1">' +
          (g.avatar
            ? '<img class="av" src="' + esc(g.avatar) + '" alt="" loading="lazy">'
            : '<span class="av ph">' + esc((g.channel || "?")[0].toUpperCase()) + '</span>') +
          '<span class="mwho"><span class="ch">' + esc(g.channel_name || g.channel) + '</span>' +
            '<span class="sub">played as ' + esc(short(g.riot_id)) + '</span></span>' +
          '<span class="badge ' + esc(g.verdict) + '">' + esc(g.verdict) + '</span>' +
        '</span>' +
        '<span class="mrow2">' +
          (me.agent_id ? '<img class="agent" src="/api/icon/agent/' + esc(me.agent_id) +
                         '.png" alt="' + esc(me.agent) + '" title="' + esc(me.agent) +
                         '" loading="lazy">' : '') +
          '<span><span class="mscore">' +
            (parts.length === 2
              ? '<span class="' + (won ? "w" : lost ? "l" : "") + '">' + esc(parts[0]) +
                '</span><span style="color:var(--faint)">&ndash;</span>' + esc(parts[1])
              : esc(g.score || "")) +
          '</span><br><span class="mmap">' + esc(g.map) + ' &middot; ' +
            esc((g.started_text || "").slice(0, 10)) + '</span></span>' +
          '<span class="mkda"><b>' + (me.kills || 0) + '/' + (me.deaths || 0) + '/' +
            (me.assists || 0) + '</b><br>' + (me.damage ? me.damage + ' dmg' : '') +
            (me.hs != null ? ' &middot; ' + me.hs + '% hs' : '') + '</span>' +
        '</span>' +
        '<span class="mrow3">' + pips(g) +
          /* Starred moments when there are any, kills when there are not.
             A match with no ace or clutch in it still has every round and
             every kill to open, and "0 to watch" on a card listing twenty
             rounds reads as a broken card rather than a quiet match. */
          '<span class="mcount">' + (g.playable
            ? '<b>' + g.playable + '</b> to watch'
            : '<b>' + (g.kill_count || 0) + '</b> kill' +
              ((g.kill_count === 1) ? '' : 's')) + '</span>' +
        '</span>' +
      '</span></button>';
  }).join("");
  $$("#matches .match").forEach(function(b){
    b.addEventListener("click", function(){ select(b.dataset.id); });
  });
}

function group(){
  for (var i = 0; i < S.groups.length; i++) {
    if (S.groups[i].id === S.sel) { return S.groups[i]; }
  }
  return null;
}

/* ================= the opened match ================= */
function renderStage(){
  var g = group(), box = $("#stageWrap");
  if (!g) { box.innerHTML = ""; return; }
  var span = g.span || {start:0, end:1};

  box.innerHTML =
    '<section class="card left">' +
      '<div class="head"><span class="t">' + esc(g.channel_name || g.channel) + '</span>' +
        '<span class="t" style="letter-spacing:.18em">' + esc(g.map) + ' &middot; ' +
          esc(g.timing) + ' timing</span></div>' +
      '<div class="player" id="player"></div>' +
      '<div class="deck">' +
        '<div class="nowline"><span class="n" id="nowname">&mdash;</span>' +
          '<span class="w" id="nowsub"></span><span class="ts mono" id="nowts"></span></div>' +
        '<div class="scrub" id="scrub"><div class="rail"></div>' +
          '<div class="playhead" id="playhead"></div><div class="tip" id="tip"></div></div>' +
        '<div class="axis"><span class="mono">' + hhmmss(span.start) + '</span>' +
          '<span class="mono">' + hhmmss(span.end) + '</span></div>' +
        '<div class="rounds" id="rounds"></div>' +
        '<div class="rndnote" id="rndnote"></div>' +
        '<ul class="moments" id="moments"></ul>' +
      '</div>' +
      (g.reasons && g.reasons.length
        ? '<div class="note">' + esc(g.reasons.join("  ·  ")) + '</div>' : '') +
    '</section>' +
    '<div class="right">' +
      '<section class="card">' +
        '<div class="head"><span class="t">Where it happened</span>' +
          '<span class="t" id="maproundlbl"></span></div>' +
        '<div class="maplegend">' +
          '<span class="key mine"><i></i>your kills</span>' +
          '<span class="key death"><i></i>your deaths</span>' +
          '<span class="key other"><i></i>everyone else</span>' +
        '</div>' +
        '<div style="padding:10px 16px 16px"><div class="mapwrap" id="mapwrap"></div></div>' +
      '</section>' +
      '<section class="card">' +
        '<div class="head"><span class="t">Scoreboard</span>' +
          '<span class="t">' + esc(g.score || "") + '</span></div>' +
        '<div style="padding:4px 16px 14px"><table class="board" id="board"></table></div>' +
      '</section>' +
    '</div>';

  renderScrub(g, span);
  renderRounds(g);
  renderMap(g);
  renderBoard(g);
  renderMoments(g);
  mountPlayer(g);
}

function renderScrub(g, span){
  var scrub = $("#scrub"), tip = $("#tip");
  var at = function(v){
    return Math.max(0, Math.min(100, (v - span.start) / Math.max(1, span.end - span.start) * 100));
  };
  var marks = [];
  (g.rounds || []).forEach(function(r){
    marks.push({cls:"round", at:r.at, label:"Round " + rno(r.round), sub:"starts", off:r.offset, ok:r.playable});
    (r.kills || []).forEach(function(k){
      marks.push({cls:"kill", at:k.at, label:k.victim || "kill",
                  sub:"round " + rno(r.round) + (k.weapon ? " / " + k.weapon : ""),
                  off:k.offset, ok:k.playable});
    });
  });
  (g.clips || []).forEach(function(c){
    marks.push({cls:"star", at:c.at, label:c.label, sub:"round " + rno(c.round),
                off:c.offset, ok:c.playable});
  });
  marks.sort(function(a, b){ return a.at - b.at; });
  marks.forEach(function(m){
    var b = document.createElement("button");
    b.type = "button"; b.className = "mark " + m.cls;
    b.style.left = at(m.at) + "%"; b.title = m.label; b.innerHTML = "<i></i>";
    if (m.ok) { b.addEventListener("click", function(){ seek(m.off, m.label, m.sub); }); }
    b.addEventListener("pointerenter", function(){
      tip.innerHTML = "<b>" + esc(m.label) + "</b><span>" + esc(m.sub) + "</span>";
      tip.style.left = at(m.at) + "%"; tip.classList.add("on");
    });
    b.addEventListener("pointerleave", function(){ tip.classList.remove("on"); });
    scrub.appendChild(b);
  });
  scrub.addEventListener("click", function(ev){
    if (ev.target.closest(".mark")) { return; }
    var box = scrub.getBoundingClientRect();
    var k = Math.max(0, Math.min(1, (ev.clientX - box.left) / box.width));
    seek(Math.round(span.start + k * (span.end - span.start)), "Scrubbed", "");
  });
}

/* ---- round timeline: win/loss per round, spike marker, click to jump ---- */
function renderRounds(g){
  var host = $("#rounds");
  var byNumber = {};
  (g.rounds || []).forEach(function(r){ byNumber[r.round] = r; });
  var all = Object.keys(byNumber).map(Number).sort(function(a, b){ return a - b; });
  if (!all.length) { host.innerHTML = '<span class="lbl">No round clock in this match</span>'; return; }
  host.innerHTML = all.map(function(n){
    var r = byNumber[n];
    var cls = r.winner ? (r.won ? "w" : "l") : "";
    return '<button class="rnd ' + cls + '" type="button" data-round="' + n + '"' +
      (r.playable ? "" : " disabled") +
      ' aria-pressed="' + String(S.round === n) + '"' +
      ' title="Round ' + rno(n) + (r.result ? " - " + esc(r.result) : "") + '">' +
      '<span class="n">' + rno(n) + '</span>' +
      (r.planted ? '<span class="spike" title="spike planted"></span>' : '') +
      '<span class="bar"></span></button>';
  }).join("");
  $$("#rounds .rnd").forEach(function(b){
    b.addEventListener("click", function(){
      var n = Number(b.dataset.round), r = byNumber[n];
      S.round = (S.round === n) ? null : n;
      $$("#rounds .rnd").forEach(function(o){
        o.setAttribute("aria-pressed", String(Number(o.dataset.round) === S.round));
      });
      paintDots();
      if (r && r.playable) { seek(r.offset, "Round " + rno(n), r.result || "start"); }
    });
  });
  var won = all.filter(function(n){ return byNumber[n].won; }).length;
  $("#rndnote").textContent = all.length + " rounds dated from the kill clock, " +
    won + " won. Click one to filter the map and jump there.";
}

/* ---- the map ---- */
function renderMap(g){
  var host = $("#mapwrap");
  if (!g.minimap || !(g.positions || []).length) {
    host.innerHTML = '<div class="nomap"><span class="eyebrow">No map</span><p>' +
      (g.minimap ? "This match was stored before kill positions were kept. Run "
                 + "<code>clipsvalorant backfill</code> to fetch them."
                 : "No minimap for " + esc(g.map) + " in the asset catalogue.") + '</p></div>';
    return;
  }
  host.innerHTML = '<img class="mapimg" alt="' + esc(g.map) +
    '" src="/api/icon/map/' + esc(g.map_id) + '.png">';
  g.positions.forEach(function(p, i){
    var b = document.createElement("button");
    b.type = "button";
    b.className = "dot " + p.kind;
    b.style.left = (p.mx * 100) + "%";
    b.style.top = (p.my * 100) + "%";
    b.dataset.round = p.round;
    b.title = (p.killer || "?") + " killed " + (p.victim || "?") +
              (p.weapon ? " with " + p.weapon : "") + " - round " + rno(p.round);
    if (p.playable) {
      b.addEventListener("click", function(){
        seek(p.offset, short(p.killer) + " killed " + short(p.victim),
             "round " + rno(p.round) + (p.weapon ? " / " + p.weapon : ""));
      });
    }
    host.appendChild(b);
  });
  paintDots();
}
function paintDots(){
  var lit = 0;
  $$("#mapwrap .dot").forEach(function(d){
    var off = S.round !== null && Number(d.dataset.round) !== S.round;
    d.classList.toggle("dim", off);
    if (!off) { lit += 1; }
  });
  var wrap = $("#mapwrap");
  if (wrap) { wrap.classList.toggle("filtered", S.round !== null); }
  var lbl = $("#maproundlbl");
  if (lbl) {
    lbl.textContent = S.round === null
      ? lit + " kills"
      : "round " + rno(S.round) + " · " + lit + (lit === 1 ? " kill" : " kills");
  }
}

/* ---- scoreboard ---- */
function renderBoard(g){
  var rows = (g.scoreboard || []).slice();
  if (!rows.length) { $("#board").innerHTML = ""; return; }
  var mine = (g.me || {}).team || "";
  rows.sort(function(a, b){
    var at = a.team === mine ? 0 : 1, bt = b.team === mine ? 0 : 1;
    return at - bt || b.score - a.score;
  });
  var html = '<thead><tr><th>Player</th><th>K</th><th>D</th><th>A</th>' +
             '<th>DMG</th><th>HS</th></tr></thead><tbody>';
  var side = rows.length ? rows[0].team : "";
  rows.forEach(function(p){
    if (p.team !== side) { html += '<tr class="sep"><td colspan="6"></td></tr>'; side = p.team; }
    html += '<tr class="' + (p.mine ? "me" : (p.team === mine ? "" : "foe")) + '">' +
      '<td><span class="who">' +
        (p.agent_id ? '<img src="/api/icon/agent/' + esc(p.agent_id) + '.png" alt="' +
                      esc(p.agent) + '" title="' + esc(p.agent) + '" loading="lazy">' : '') +
        '<span>' + esc(p.name) + '</span></span></td>' +
      '<td>' + p.kills + '</td><td>' + p.deaths + '</td><td>' + p.assists + '</td>' +
      '<td>' + (p.damage || "") + '</td>' +
      '<td>' + (p.hs != null ? p.hs + "%" : "") + '</td></tr>';
  });
  $("#board").innerHTML = html + '</tbody>';
}

function renderMoments(g){
  $("#moments").innerHTML = (g.clips || []).map(function(c){
    return '<li><button class="moment" type="button" data-off="' + c.offset + '"' +
      (c.playable ? "" : " disabled") + '>' +
      '<svg class="star-ico" viewBox="0 0 24 24" fill="currentColor"><path d="m12 2 2.9 6.3 6.8.8-5 4.6 1.3 6.7L12 17l-6 3.4 1.3-6.7-5-4.6 6.8-.8z"/></svg>' +
      '<span><span class="lab">' + esc(c.label) + '</span> ' +
      '<span class="sub">round ' + rno(c.round) + '</span></span>' +
      '<span class="st mono">' + esc(c.playable ? c.stamp : "outside") + '</span></button></li>';
  }).join("") || '<li class="lbl" style="padding:6px 8px">No standout moments in this one</li>';
  $$("#moments .moment").forEach(function(b){
    b.addEventListener("click", function(){
      var lab = $(".lab", b);
      seek(Number(b.dataset.off), lab ? lab.textContent : "Moment", "");
    });
  });
}

/* ---- Twitch embed ---- */
function mountPlayer(g){
  var host = $("#player");
  if (!host) { return; }
  if (S.tick) { clearInterval(S.tick); S.tick = null; }
  S.player = null;
  var first = (g.clips || []).filter(function(c){ return c.playable; })[0]
           || (g.rounds || []).filter(function(r){ return r.playable; })[0];
  var start = first ? first.offset : 0;
  function iframe(at){
    host.innerHTML = '<iframe allowfullscreen src="https://player.twitch.tv/?video=v' +
      encodeURIComponent(g.vod_id) + "&time=" + hhmmss(at).replace(/:/g, "") +
      "&parent=" + encodeURIComponent(location.hostname) + '"></iframe>';
  }
  function build(){
    try {
      host.innerHTML = "";
      S.player = new Twitch.Player("player", {
        video: g.vod_id, time: hhmmss(start), width: "100%", height: "100%",
        parent: [location.hostname], autoplay: false
      });
      S.tick = setInterval(function(){
        if (!S.player || !S.player.getCurrentTime) { return; }
        var t = S.player.getCurrentTime();
        if (typeof t !== "number" || isNaN(t)) { return; }
        var span = g.span, head = $("#playhead"), ts = $("#nowts");
        if (!head) { return; }
        var k = (t - span.start) / Math.max(1, span.end - span.start);
        head.style.left = Math.max(0, Math.min(100, k * 100)) + "%";
        head.classList.toggle("on", k >= -0.02 && k <= 1.02);
        if (ts) { ts.textContent = hhmmss(t); }
      }, 500);
    } catch (e) { iframe(start); }
  }
  if (window.Twitch && window.Twitch.Player) { build(); return; }
  var s = document.createElement("script");
  s.src = "https://embed.twitch.tv/embed/v1.js";
  s.onload = build;
  s.onerror = function(){ iframe(start); };
  document.head.appendChild(s);
  setTimeout(function(){ if (!S.player && !host.children.length) { iframe(start); } }, 4000);
}

function seek(sec, label, sub){
  var n = $("#nowname"), s = $("#nowsub");
  if (n) { n.textContent = label || ""; }
  if (s) { s.textContent = sub || ""; }
  if (S.player && S.player.seek) {
    try { S.player.seek(sec); if (S.player.play) { S.player.play(); } return; } catch (e) {}
  }
  var g = group(); if (!g) { return; }
  var host = $("#player");
  if (host) {
    host.innerHTML = '<iframe allowfullscreen src="https://player.twitch.tv/?video=v' +
      encodeURIComponent(g.vod_id) + "&time=" + hhmmss(sec).replace(/:/g, "") +
      "&parent=" + encodeURIComponent(location.hostname) + '"></iframe>';
  }
}

function select(id){
  var changed = S.sel !== id;
  S.sel = id;
  if (changed) { S.round = null; }
  $$("#matches .match").forEach(function(b){
    b.setAttribute("aria-current", String(b.dataset.id === id));
  });
  renderStage();
}

/* ---- data ---- */
function api(path, opts){
  return fetch(path, opts).then(function(r){
    return r.json().then(function(j){
      if (!r.ok) { throw new Error(j.error || r.status); }
      return j;
    });
  });
}
function fail(msg){
  var e = $("#err");
  if (!msg) { e.hidden = true; return; }
  e.hidden = false; e.textContent = msg;
}
function loadClips(key){
  return api("/api/clips?key=" + encodeURIComponent(key)).then(function(j){
    S.key = j.key; S.groups = j.groups || [];
    /* The chip is the region of the account on screen, not the server's
       own setting. A scan works out the shard from the Riot ID itself and
       overrides whatever this machine was configured for, so the old chip
       told every visitor NA no matter who they were or where they play. */
    var sum = j.summary || {};
    var chip = $("#region");
    if (sum.region) { chip.hidden = false; chip.textContent = sum.region; }
    else { chip.hidden = true; }
    if (sum.riot_id) { nameplate(sum.riot_id); }
    renderTotals(); renderMatches();
    if (!group()) { S.sel = S.groups.length ? S.groups[0].id : null; S.round = null; }
    if (S.sel) { select(S.sel); } else { $("#stageWrap").innerHTML = ""; }
    $("#footright").textContent = ((j.summary && j.summary.matches) || 0) + " matches stored";
  });
}
function nameplate(riot){
  var bits = String(riot || "").split("#");
  $("#who").innerHTML = riot
    ? esc(bits[0]) + '<span class="tag">#' + esc(bits[1] || "") + "</span>"
    : "Find your plays";
}
function boot(){
  splash();
  api("/api/state").then(function(j){
    /* Deliberately not the server's own Riot ID. On one machine that was a
       convenience; on a URL handed to friends it is a loaded gun - press
       Scan without looking and you have spent the queue's turn on somebody
       else's account. Yours, if this browser has scanned before. */
    var mine = "";
    try { mine = localStorage.getItem("vg:riot") || ""; } catch(e){}
    $("#riot").value = mine;
    nameplate(mine || j.default_riot_id);
    if (!j.has_twitch) { fail("No Twitch credentials configured, so nothing can be confirmed from a broadcast."); }
    S.ticket = readTicket();
    if (S.ticket) { watch(); }
    queueChip();
    S.qtick = setInterval(queueChip, 6000);
    var key = "";
    try { key = localStorage.getItem("vg:key") || ""; } catch(e){}
    return loadClips(key || j.default_key || "");
  }).catch(function(e){ fail(String(e.message || e)); });
}

/* ---- the splash -----------------------------------------------------
   Once per browser session rather than once per load: a brand animation
   you cannot skip is charming the first time and an obstacle every time
   after. Any click or key cuts it short, and reduced-motion never sees it.
   --------------------------------------------------------------------- */
function splash(){
  var el = $("#splash");
  if (!el) { return; }
  var seen = false;
  try { seen = sessionStorage.getItem("vg:splash") === "1"; } catch(e){}
  if (seen || matchMedia("(prefers-reduced-motion: reduce)").matches) { el.remove(); return; }
  try { sessionStorage.setItem("vg:splash", "1"); } catch(e){}
  el.hidden = false;
  document.documentElement.style.overflow = "hidden";
  var end = 24 * 60 + 8, t0 = Date.now();
  var tick = setInterval(function(){
    var p = Math.min(1, (Date.now() - t0) / 1250), sec = Math.round(end * p);
    $("#sptc").textContent = "00:" + pad2(Math.floor(sec / 60)) + ":" + pad2(sec % 60);
    if (p >= 1) { clearInterval(tick); }
  }, 40);
  var leave = function(){
    clearInterval(tick);
    el.classList.add("gone");
    document.documentElement.style.overflow = "";
    setTimeout(function(){ if (el.parentNode) { el.remove(); } }, 500);
    removeEventListener("keydown", leave);
  };
  addEventListener("keydown", leave);
  el.addEventListener("click", leave);
  setTimeout(leave, 1900);
}

/* ---- the state of the line, for anyone looking ---- */
function queueChip(){
  api("/api/queue").then(function(q){
    var chip = $("#qchip"), running = (q.running || []).length, waiting = q.waiting || 0;
    if (!running && !waiting) { chip.hidden = true; return; }
    chip.hidden = false;
    chip.textContent = running
      ? (waiting ? running + " scanning · " + waiting + " waiting" : running + " scanning")
      : waiting + " waiting";
  }).catch(function(){});
}

/* ---- scan, queue and progress ---------------------------------------
   Three states share one poll. Queued raises the waiting screen; running
   paints the progress bar exactly as it always did; anything terminal
   clears the ticket and reloads the clips. The ticket lives in
   localStorage, so closing the tab and coming back finds your place in
   the line instead of losing it.
   --------------------------------------------------------------------- */
var PHASES = {starting:"Starting", ingest:"Reading match history",
              resolve:"Checking the lobby", done:"Done"};
var TICKET = "vg:ticket";

function readTicket(){ try { return localStorage.getItem(TICKET) || ""; } catch(e){ return ""; } }
function saveTicket(t){ S.ticket = t || ""; try { localStorage.setItem(TICKET, S.ticket); } catch(e){} }
function dropTicket(){ S.ticket = ""; try { localStorage.removeItem(TICKET); } catch(e){} }

function watch(){
  if (!S.ticket) { return; }
  if (S.poll) { clearInterval(S.poll); }
  S.poll = setInterval(poll, 900);
  poll();
}
function unwatch(){
  if (S.poll) { clearInterval(S.poll); S.poll = null; }
  $("#go").disabled = false;
  $("#stop").hidden = true;
}

/* ---- the waiting screen ---- */
function railTicks(){
  var rail = $("#qrail");
  if (rail.dataset.ticked) { return; }
  rail.dataset.ticked = "1";
  for (var p = 0; p <= 100; p += 25) {
    var t = document.createElement("i");
    t.style.left = p + "%";
    rail.appendChild(t);
  }
}
function paintGate(j){
  railTicks();
  var gate = $("#gate");
  if (!S.gateOut) { gate.hidden = false; gate.classList.remove("gone"); }
  $("#qback").hidden = !S.gateOut;
  $("#prog").hidden = true;
  $("#go").disabled = true;
  $("#stop").hidden = true;

  var pos = j.position || 1, ahead = j.ahead || 0;
  $("#gatepos").textContent = pad2(pos);
  $("#qbackpos").textContent = "#" + pos;
  $("#gatenote").textContent = ahead === 0
    ? "You are next. Yours starts the moment the running one finishes."
    : ahead + (ahead === 1 ? " scan" : " scans") + " ahead of you";
  $("#qeta").textContent = j.eta ? "~" + Math.max(1, Math.round(j.eta / 60)) + " MIN" : "";
  /* The marker walks leftwards as the line moves. It is distance left to
     go, not a percentage of anything, which is why zero ahead sits at the
     start of the rail rather than the end of it. */
  $("#qme").style.left = Math.round(ahead / (ahead + 1) * 100) + "%";

  var now = (j.now || [])[0];
  $("#gatenow").innerHTML = now
    ? "Running now: <b>" + esc(short(now.riot_id)) + "</b><br>"
      + esc(PHASES[now.phase] || now.phase || "working")
      + (now.total ? " · " + now.done + " / " + now.total : "")
    : "Nothing is running. Yours starts in a moment.";
}
function hideGate(){
  var gate = $("#gate");
  if (!gate.hidden) {
    gate.classList.add("gone");
    setTimeout(function(){ gate.hidden = true; gate.classList.remove("gone"); }, 420);
  }
  $("#qback").hidden = true;
  S.gateOut = false;
}

function poll(){
  api("/api/job?ticket=" + encodeURIComponent(S.ticket)).then(function(j){
    /* Unknown covers a ticket from before a restart and one that has aged
       out. Either way the page belongs back in a state you can use. */
    if (!j || j.state === "idle" || j.state === "unknown") {
      unwatch(); hideGate(); dropTicket(); return;
    }
    if (j.state === "queued") { paintGate(j); return; }

    hideGate();
    $("#prog").hidden = false;
    $("#go").disabled = true;
    if (j.state === "running") { $("#stop").hidden = false; $("#stop").disabled = false; }
    $("#pphase").textContent = PHASES[j.phase] || j.phase || "Working";
    var fill = $("#pfill");
    if (j.total > 0) {
      fill.classList.remove("indet");
      fill.style.width = Math.round(j.done / j.total * 100) + "%";
      $("#ppct").textContent = j.done + " / " + j.total + "  " + j.elapsed + "s";
    } else {
      fill.classList.add("indet"); fill.style.width = "";
      $("#ppct").textContent = j.elapsed + "s";
    }
    var notes = $("#notes");
    if (notes) {
      notes.innerHTML = (j.notes || []).map(function(n){
        return "<div>" + esc(n) + "</div>";
      }).join("");
    }
    var log = $("#log");
    log.textContent = (j.lines || []).join("\n");
    log.scrollTop = log.scrollHeight;
    if (j.stopping) { $("#pphase").textContent = "Stopping"; }
    if (j.state !== "running") {
      unwatch();
      dropTicket();
      fill.classList.remove("indet");
      fill.style.width = j.state === "cancelled" ? fill.style.width : "100%";
      $("#pphase").textContent = j.state === "error" ? "Failed"
                              : j.state === "cancelled" ? "Stopped" : "Done";
      if (j.error && j.state === "error") { fail(j.error); }
      /* Remember whose results these are, so this browser comes back to
         its own account rather than to whoever the server belongs to. */
      if (j.key) { try { localStorage.setItem("vg:key", j.key); } catch(e){} }
      loadClips(j.key || S.key).catch(function(e){ fail(String(e.message || e)); });
    }
  }).catch(function(){});
}

$("#scanform").addEventListener("submit", function(ev){
  ev.preventDefault();
  fail("");
  var riot = $("#riot").value.trim();
  if (!riot) { fail("Enter a Riot ID as name#tag."); return; }
  api("/api/search", {
    method: "POST", headers: {"Content-Type": "application/json"},
    /* The ceiling has to follow the window, or picking 14 days quietly
       gives you eight. Listing is cheap - one call per hundred matches, and
       only the ones that are new AND inside the window get fetched in detail
       - so this asks for the whole window and lets the window end the walk. */
    body: JSON.stringify({riot_id: riot, days: S.days, watch: true,
                          mode: S.mode,
                          fetch: Math.min(250, Math.max(40, S.days * 8))})
  }).then(function(j){
    $("#log").textContent = "";
    S.gateOut = false;
    try { localStorage.setItem("vg:riot", riot); } catch(e){}
    nameplate(riot);
    saveTicket(j.id || "");
    if (j.state === "queued") { paintGate(j); }
    watch();
  }).catch(function(e){ $("#go").disabled = false; fail(String(e.message || e)); });
});

function abandon(btn){
  if (btn) { btn.disabled = true; }
  return api("/api/cancel", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ticket: S.ticket})
  }).catch(function(e){
    if (btn) { btn.disabled = false; }
    fail(String(e.message || e));
  });
}

$("#stop").addEventListener("click", function(){
  $("#pphase").textContent = "Stopping";
  abandon(this);
});

$("#gateleave").addEventListener("click", function(){
  var btn = this;
  abandon(btn).then(function(){
    unwatch(); hideGate(); dropTicket();
    btn.disabled = false;
  });
});

/* Stepping out of the waiting screen, not out of the queue: the ticket
   keeps its place and the pill at the bottom says where it is. */
$("#gatebrowse").addEventListener("click", function(){
  S.gateOut = true;
  var gate = $("#gate");
  gate.classList.add("gone");
  setTimeout(function(){ gate.hidden = true; gate.classList.remove("gone"); }, 420);
  $("#qback").hidden = false;
});
$("#qbackopen").addEventListener("click", function(){
  S.gateOut = false;
  $("#qback").hidden = true;
  $("#gate").hidden = false;
  $("#gate").classList.remove("gone");
});

$("#logbtn").addEventListener("click", function(){
  var log = $("#log"), open = log.hidden;
  log.hidden = !open;
  this.setAttribute("aria-expanded", String(open));
  this.textContent = open ? "Hide log" : "Show log";
});

addEventListener("keydown", function(e){
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") {
    if (e.key === "Escape") { e.target.blur(); }
    return;
  }
  if (e.key === "/") { e.preventDefault(); $("#riot").focus(); $("#riot").select(); }
  if (e.key === "Escape" && S.round !== null) {
    S.round = null;
    $$("#rounds .rnd").forEach(function(o){ o.setAttribute("aria-pressed", "false"); });
    paintDots();
  }
  if (e.key === "j" || e.key === "k") {
    var gs = visible();
    if (!gs.length) { return; }
    var i = 0;
    for (var n = 0; n < gs.length; n++) { if (gs[n].id === S.sel) { i = n; } }
    i = Math.max(0, Math.min(gs.length - 1, i + (e.key === "j" ? 1 : -1)));
    select(gs[i].id);
    var el = $('#matches .match[data-id="' + gs[i].id + '"]');
    if (el) { el.scrollIntoView({block: "nearest"}); }
  }
});

boot();
</script>
</body>
</html>
"""
