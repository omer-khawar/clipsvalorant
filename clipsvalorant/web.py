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

import dataclasses
import json
import logging
import re
import socket
import sqlite3
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from clipsvalorant import calibrate, db
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
DEFAULT_FETCH = 40
MAX_FETCH = 100

MAX_JOB_LINES = 400

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
       m.started_at_epoch, m.map, m.mode, m.queue,
       m.rounds_won, m.rounds_lost, m.my_team_won,
       v.created_epoch, v.duration_s, v.title,
       h.round, h.label, h.kind, h.kill_count, h.time_in_match_ms, h.round_won,
       o.shift_s, o.method
FROM encounter e
JOIN match m      ON m.id = e.match_id
JOIN twitch_vod v ON v.id = e.vod_id
JOIN highlight h  ON h.match_id = e.match_id
LEFT JOIN match_vod_offset o
       ON o.match_id = e.match_id AND o.vod_id = e.vod_id
WHERE e.vod_id IS NOT NULL AND e.verdict IN ('confirmed', 'likely')
ORDER BY m.started_at_epoch DESC, h.time_in_match_ms ASC
"""


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


def clip_groups(conn: sqlite3.Connection, *, lead: int = LEAD_S) -> list[dict[str, Any]]:
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
    for group in groups.values():
        match_id = group["match_id"]
        if match_id not in timelines:
            timelines[match_id] = match_timeline(conn, match_id)
        _attach_timeline(
            group, timelines[match_id], created.get(group["vod_id"], 0), lead=lead
        )

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
        "  WHERE verdict IN ('confirmed', 'likely')) AS found"
    ).fetchone()
    return dict(row)


# ---------------------------------------------------------------- jobs

@dataclass
class Job:
    """One search, running on its own thread, watched by polling."""

    id: str
    riot_id: str
    key: str
    state: str = "running"  # running | done | error
    phase: str = "starting"  # starting | ingest | resolve | done
    done: int = 0
    total: int = 0
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    error: str = ""
    lines: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

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
            phase, done, total = self.phase, self.done, self.total
        return {
            "id": self.id,
            "riot_id": self.riot_id,
            "key": self.key,
            "state": self.state,
            "phase": phase,
            "done": done,
            "total": total,
            "error": self.error,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
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
    from clipsvalorant.ingest import ingest
    from clipsvalorant.resolve import resolve_recent

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
        job.step("ingest")
        job.say(f"Looking up {acct.riot_id} in {acct.region.upper()}...")
        report = ingest(acct, limit=fetch, mode=mode)
        job.say(report.summary())
        for err in report.errors[:5]:
            job.say(f"! {err}")

        cutoff = int(time.time()) - days * 86400
        with db.session(acct.db_path) as conn:
            ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM match WHERE started_at_epoch >= ?"
                    " ORDER BY started_at_epoch DESC",
                    (cutoff,),
                )
            ]
            newest = conn.execute(
                "SELECT MAX(started_at_epoch) AS newest FROM match"
            ).fetchone()["newest"]

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

        if len(ids) >= fetch:
            job.say(
                f"Stopped at the {fetch}-match ceiling, so the last {days} days "
                "may hold more than this."
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

        def tick(match_id: str, report: Any) -> None:
            nonlocal done
            done += 1
            job.step("resolve", done=done)
            job.say(
                f"match {done}/{len(ids)} checked - "
                f"{len(report.confirmed)} confirmed, {len(report.candidates)} unverified"
            )

        found = resolve_recent(
            acct, match_ids=ids, watch_vods=watch, on_match=tick
        )
        job.say(found.summary())
        for err in found.errors[:5]:
            job.say(f"! {err}")
        job.step("done", done=len(ids), total=len(ids))
        job.state = "done"
    except Exception as exc:  # the thread is the only place this can be caught
        job.state = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.say(f"Failed: {job.error}")
        log.exception("Search for %s failed", job.riot_id)
    finally:
        job.finished = time.time()
        package.removeHandler(handler)
        package.setLevel(previous)


@dataclass
class State:
    """What the handler is allowed to touch. One search at a time."""

    cfg: Config
    job: Job | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    runner: Callable[..., None] = run_search

    @property
    def data_dir(self) -> Path:
        return self.cfg.db_path.parent

    def busy(self) -> bool:
        with self._lock:
            return self.job is not None and self.job.state == "running"

    def start(self, riot_id: str, **kwargs: Any) -> Job:
        """Begin a search, or raise RuntimeError if one is already running.

        Serialised deliberately. Two searches at once would race for the same
        API quota and, for the same account, the same database rows.
        """
        with self._lock:
            if self.job is not None and self.job.state == "running":
                raise RuntimeError(
                    f"A search for {self.job.riot_id} is still running."
                )
            job = Job(id=f"job-{int(time.time() * 1000)}", riot_id=riot_id, key="")
            self.job = job
        thread = threading.Thread(
            target=self.runner, args=(self.cfg, job), kwargs=kwargs, daemon=True
        )
        thread.start()
        return job


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

        # -- routes

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
            url = urlparse(self.path)
            query = parse_qs(url.query)
            route = url.path.rstrip("/") or "/"
            try:
                if route == "/":
                    self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif route == "/api/state":
                    self._json(self._state_payload())
                elif route == "/api/clips":
                    self._json(self._clips_payload(query.get("key", [""])[0]))
                elif route == "/api/job":
                    job = state.job
                    self._json(job.snapshot() if job else {"state": "idle"})
                else:
                    self._json({"error": "no such endpoint"}, 404)
            except Exception as exc:
                log.exception("GET %s failed", self.path)
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        do_HEAD = do_GET

        def do_POST(self) -> None:  # noqa: N802
            route = urlparse(self.path).path.rstrip("/") or "/"
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
                job = state.start(
                    riot_id,
                    days=days,
                    fetch=fetch,
                    mode=mode,
                    watch=bool(body.get("watch", True)),
                    region=region,
                )
            except RuntimeError as exc:
                self._json({"error": str(exc)}, 409)
                return
            self._json(job.snapshot(), 202)

        # -- payloads

        def _state_payload(self) -> dict[str, Any]:
            cfg = state.cfg
            job = state.job
            return {
                "default_riot_id": cfg.riot_id,
                "default_key": cfg.db_path.stem,
                "default_days": DEFAULT_DAYS,
                "max_days": MAX_DAYS,
                "region": cfg.region,
                "has_twitch": cfg.has_twitch,
                "accounts": known_accounts(state.data_dir),
                "job": job.snapshot() if job else None,
            }

        def _clips_payload(self, key: str) -> dict[str, Any]:
            key = key or state.cfg.db_path.stem
            path = path_for_key(state.data_dir, key)
            if path is None:
                return {"key": key, "groups": [], "summary": {},
                        "error": "nothing stored for that account yet"}
            with _read_only(path) as conn:
                return {
                    "key": key,
                    "groups": clip_groups(conn),
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
    """Run the UI until interrupted."""
    state = State(cfg=cfg)
    port = free_port(port, host)
    server = ThreadingHTTPServer((host, port), make_handler(state))
    server.daemon_threads = True
    # Twitch's player refuses to embed unless `parent` matches the page's own
    # hostname, and it does not accept a bare IP. The page builds that from
    # location.hostname, so the URL offered here has to be the name, not the
    # address it resolves to.
    url = f"http://localhost:{port}/"
    say(f"clipsvalorant UI on {url}")
    say(f"  database: {cfg.db_path}")
    say(f"  account:  {cfg.riot_id} ({cfg.region.upper()})")
    if not cfg.has_twitch:
        say("  ! Twitch credentials missing - searching will stop after ingest.")
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


# ---------------------------------------------------------------- the page

PAGE = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>clipsvalorant</title>
<style>
/* ---- tokens -------------------------------------------------------- */
:root{
  color-scheme: light;
  --bg:#fbfbfc; --panel:#ffffff; --panel-2:#f4f4f6; --inset:#eeeef1;
  --fg:#0c0c10; --muted:#64646f; --faint:#9c9ca6; --line:#e5e5ea;
  --accent:#e0303f; --accent-fg:#ffffff; --accent-soft:rgba(224,48,63,.10);
  --good:#15803d; --good-soft:rgba(21,128,61,.10);
  --warn:#a16207; --warn-soft:rgba(161,98,7,.10);
  --shadow:0 1px 2px rgba(12,12,16,.05), 0 10px 28px -14px rgba(12,12,16,.22);
  --radius:14px; --radius-sm:9px;
}
html[data-theme="dark"]{
  color-scheme: dark;
  --bg:#08080a; --panel:#111116; --panel-2:#17171d; --inset:#1d1d25;
  --fg:#f3f3f6; --muted:#9a9aa6; --faint:#63636f; --line:#232430;
  --accent:#ff4655; --accent-fg:#1a0509; --accent-soft:rgba(255,70,85,.14);
  --good:#4ade80; --good-soft:rgba(74,222,128,.12);
  --warn:#e3b341; --warn-soft:rgba(227,179,65,.12);
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 16px 40px -20px rgba(0,0,0,.9);
}

*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
  font-variant-numeric:tabular-nums;
  transition:background .3s ease, color .3s ease;
}
a{color:inherit}
button{font:inherit;color:inherit}
::selection{background:var(--accent-soft)}

/* ---- motion -------------------------------------------------------- */
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes slide{0%{transform:translateX(-110%)}100%{transform:translateX(420%)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
@keyframes pop{from{transform:scale(.97);opacity:0}to{transform:none;opacity:1}}
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation:none !important;transition:none !important}
}

/* ---- chrome -------------------------------------------------------- */
header{
  position:sticky; top:0; z-index:40;
  background:color-mix(in srgb, var(--bg) 82%, transparent);
  backdrop-filter:saturate(180%) blur(14px);
  border-bottom:1px solid var(--line);
}
.bar{
  max-width:1360px; margin:0 auto; padding:13px 22px;
  display:flex; align-items:center; gap:12px; flex-wrap:wrap;
}
.brand{display:flex;align-items:center;gap:9px;font-weight:600;letter-spacing:-.02em}
.brand i.dot{
  width:9px;height:9px;border-radius:3px;background:var(--accent);
  box-shadow:0 0 0 4px var(--accent-soft);
}
.brand span{color:var(--muted);font-weight:450}

form.search{display:flex;align-items:center;gap:8px;margin-left:auto;flex-wrap:wrap}
.field{
  display:flex;align-items:center;gap:8px;
  background:var(--panel); border:1px solid var(--line);
  border-radius:var(--radius-sm); padding:0 11px; height:36px;
  transition:border-color .18s ease, box-shadow .18s ease;
}
.field:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.field svg{color:var(--faint);flex:none}
.field input,.field select{
  border:0;background:transparent;outline:none;color:inherit;height:100%;
}
.field input{width:170px}
.field select{cursor:pointer}
select.field{max-width:230px}
.btn{
  height:36px;padding:0 15px;border-radius:var(--radius-sm);border:1px solid transparent;
  background:var(--accent);color:var(--accent-fg);font-weight:550;cursor:pointer;
  transition:transform .14s ease, filter .18s ease, opacity .18s ease;
}
.btn:hover{filter:brightness(1.07)}
.btn:active{transform:translateY(1px)}
.btn[disabled]{opacity:.5;cursor:default;transform:none;filter:none}
.icon-btn{
  width:36px;height:36px;display:grid;place-items:center;flex:none;
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius-sm);
  cursor:pointer;transition:background .18s ease,border-color .18s ease,transform .2s ease;
}
.icon-btn:hover{background:var(--panel-2);border-color:var(--faint)}
.icon-btn:active{transform:scale(.94)}
.icon-btn svg{transition:transform .4s cubic-bezier(.2,.7,.3,1)}
.icon-btn:hover svg{transform:rotate(18deg)}
.icon-btn .moon{display:none}
html[data-theme="dark"] .icon-btn .sun{display:none}
html[data-theme="dark"] .icon-btn .moon{display:block}

/* ---- progress ------------------------------------------------------ */
.rail{height:2px;overflow:hidden;position:relative}
.rail .fill{
  height:100%;width:0;background:var(--accent);border-radius:2px;
  transition:width .45s cubic-bezier(.2,.7,.3,1);
}
.rail.indet .fill{width:28%;animation:slide 1.25s cubic-bezier(.5,0,.5,1) infinite}

.status{
  max-width:1360px;margin:0 auto;padding:0 22px;
  display:none;align-items:center;gap:11px;height:46px;
}
.status.on{display:flex}
.status i.spark{width:7px;height:7px;border-radius:50%;background:var(--accent);
  flex:none;animation:pulse 1.4s infinite}
.status .msg{color:var(--muted);font-size:13.5px;flex:1;min-width:0;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.status .count{color:var(--faint);font-size:12.5px;flex:none}
.linkish{
  background:none;border:0;padding:0;color:var(--muted);font-size:12.5px;
  cursor:pointer;text-decoration:underline;text-underline-offset:3px;
  text-decoration-color:var(--line);flex:none;
}
.linkish:hover{color:var(--fg)}

/* Narrow enough that the bar would wrap into three ragged rows. Brand and
   theme take one line, the search form takes the next at full width. */
@media (max-width:760px){
  .bar{gap:10px;padding:11px 16px}
  .brand{order:1}
  #theme{order:2;margin-left:auto}
  form.search{order:3;width:100%;margin-left:0;gap:7px}
  form.search label.field:first-of-type{flex:1 1 140px;min-width:0}
  .field input{width:100%;min-width:0}
  /* min-width:0 is what lets it share a row: a select's intrinsic width is
     its longest option, which on its own pushes the button onto a line of
     its own. */
  select.field{flex:1 1 110px;max-width:none;min-width:0}
  form.search .btn{flex:0 0 auto}
}

/* ---- layout -------------------------------------------------------- */
main{max-width:1360px;margin:0 auto;padding:22px}
.split{display:grid;grid-template-columns:320px minmax(0,1fr);gap:20px;align-items:start}
@media (max-width:1040px){
  .split{grid-template-columns:1fr}
  .finds{max-height:260px}
}
@media (max-width:760px){main{padding:16px}}

.panel{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);overflow:hidden;
}
.rowhead{
  display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:13px 15px;border-bottom:1px solid var(--line);
}
.rowhead h2{margin:0;font-size:11.5px;font-weight:650;letter-spacing:.09em;
  text-transform:uppercase;color:var(--faint)}
.pill{
  font-size:11.5px;font-weight:650;padding:3px 8px;border-radius:999px;
  background:var(--inset);color:var(--muted);
}

/* ---- find list ----------------------------------------------------- */
.finds{max-height:calc(100vh - 180px);overflow:auto;padding:7px}
.find{
  display:block;width:100%;text-align:left;cursor:pointer;
  background:transparent;border:1px solid transparent;border-radius:var(--radius-sm);
  padding:11px 12px;margin:1px 0;
  transition:background .18s ease,border-color .18s ease;
  animation:rise .4s cubic-bezier(.2,.7,.3,1) both;
}
.find:hover{background:var(--panel-2)}
.find[aria-current="true"]{background:var(--accent-soft);border-color:var(--accent)}
.find .top{display:flex;align-items:center;gap:8px}
.find .who{font-weight:580;letter-spacing:-.01em;flex:1;min-width:0;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.find .sub{display:flex;align-items:center;gap:6px;margin-top:4px;
  color:var(--muted);font-size:12.5px}
.find .sub b{font-weight:550;color:var(--faint)}
.tag{
  font-size:10px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
  padding:3px 6px;border-radius:5px;flex:none;white-space:nowrap;
}
.tag.confirmed{background:var(--good-soft);color:var(--good)}
.tag.likely{background:var(--warn-soft);color:var(--warn)}
.tag.team{background:var(--inset);color:var(--muted)}

/* ---- theatre ------------------------------------------------------- */
.theatre{animation:pop .34s cubic-bezier(.2,.7,.3,1) both}
.stage{position:relative;background:#000;overflow:hidden;aspect-ratio:16/9}
.stage iframe,.stage>div:not(.placeholder){position:absolute;inset:0;
  width:100%;height:100%;border:0}
.stage .placeholder{
  position:absolute;inset:0;display:grid;place-items:center;
  color:#80808c;font-size:13.5px;text-align:center;padding:20px;
}

.meta{display:flex;align-items:center;gap:10px;padding:13px 15px;flex-wrap:wrap}
.meta .title{font-weight:620;letter-spacing:-.01em}
.meta .sep{color:var(--line)}
.meta .dim{color:var(--muted);font-size:13px}
.meta .right{margin-left:auto;display:flex;align-items:center;gap:9px;flex-wrap:wrap}

/* ---- scrubber ------------------------------------------------------ */
.scrubwrap{padding:0 15px 14px}
.scrub{position:relative;height:38px;cursor:pointer}
.scrub .track{position:absolute;left:0;right:0;top:18px;height:4px;
  border-radius:3px;background:var(--inset)}
.scrub .played{position:absolute;left:0;top:18px;height:4px;border-radius:3px;
  width:0;background:var(--accent);opacity:.5;transition:width .4s linear}
.scrub i.tick{position:absolute;top:14px;width:1px;height:12px;
  background:var(--line);transform:translateX(-.5px)}
.mark{position:absolute;top:20px;transform:translate(-50%,-50%);
  border:0;padding:0;background:transparent;cursor:pointer;line-height:0}
.mark i{display:block;border-radius:50%;background:var(--muted);
  width:7px;height:7px;transition:transform .16s ease,opacity .16s ease}
.mark:hover i{transform:scale(1.7)}
.mark.kill i{background:var(--fg);opacity:.4}
.mark.kill:hover i{opacity:1}
.mark.star i{width:12px;height:12px;background:var(--accent);
  box-shadow:0 0 0 3px var(--accent-soft)}
.mark.death{cursor:default}
.mark.death i{width:6px;height:6px;background:transparent;border-radius:2px;
  border:1.5px solid var(--faint);transform:rotate(45deg)}
.mark.death:hover i{transform:rotate(45deg)}
.mark.out{opacity:.2;cursor:not-allowed}
.mark.out:hover i{transform:none}
.scrub i.head{position:absolute;top:10px;width:2px;height:20px;border-radius:2px;
  background:var(--accent);transform:translateX(-1px);left:0;
  transition:left .4s linear;pointer-events:none}
.ends{display:flex;justify-content:space-between;color:var(--faint);font-size:11.5px}

/* ---- moments ------------------------------------------------------- */
.tabs{display:flex;gap:3px;padding:3px;background:var(--inset);border-radius:9px}
.tabs button{
  border:0;background:transparent;padding:5px 11px;border-radius:7px;
  font-size:12.5px;font-weight:550;color:var(--muted);cursor:pointer;
  transition:background .18s ease,color .18s ease;white-space:nowrap;
}
.tabs button[aria-selected="true"]{background:var(--panel);color:var(--fg);
  box-shadow:0 1px 2px rgba(0,0,0,.16)}

.moments{max-height:310px;overflow:auto;padding:5px 7px 10px;
  border-top:1px solid var(--line)}
.moment{
  display:flex;align-items:center;gap:11px;width:100%;text-align:left;
  background:transparent;border:0;border-radius:var(--radius-sm);
  padding:8px 10px;cursor:pointer;transition:background .16s ease;
  animation:fade .3s ease both;
}
.moment:hover{background:var(--panel-2)}
.moment[aria-current="true"]{background:var(--accent-soft)}
.moment .stamp{font-size:12px;color:var(--muted);background:var(--inset);
  padding:2.5px 7px;border-radius:6px;flex:none}
.moment .what{flex:1;min-width:0;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;font-size:13.5px}
.moment .what em{font-style:normal;color:var(--muted)}
.moment .badge{font-size:11px;color:var(--faint);flex:none}
.moment.out{opacity:.42;cursor:not-allowed}
.moment.out:hover{background:transparent}
.roundhead{display:flex;align-items:center;gap:9px;padding:10px 10px 5px;
  font-size:10.5px;font-weight:650;letter-spacing:.08em;text-transform:uppercase;
  color:var(--faint)}
.roundhead i.line{flex:1;height:1px;background:var(--line)}

/* ---- empty / notes ------------------------------------------------- */
.empty{padding:56px 26px;text-align:center;color:var(--muted);
  animation:fade .4s ease both}
.empty .big{font-size:17px;color:var(--fg);font-weight:580;letter-spacing:-.01em}
.empty p{margin:9px auto 0;max-width:430px;font-size:13.5px;line-height:1.65}
.empty svg{color:var(--faint);margin-bottom:14px}
.moments .empty{padding:26px 16px;font-size:13px}

details.why{margin:0 15px 15px;font-size:13px}
details.why summary{cursor:pointer;color:var(--muted);font-size:12.5px;
  list-style:none;padding:6px 0;user-select:none}
details.why summary::-webkit-details-marker{display:none}
details.why summary:hover{color:var(--fg)}
details.why ul{margin:4px 0 0;padding-left:18px;color:var(--muted);line-height:1.7}
.log{
  margin:18px 0 0;padding:12px 14px;background:var(--panel);
  border:1px solid var(--line);border-radius:var(--radius);
  font:12px/1.65 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  color:var(--muted);max-height:240px;overflow:auto;white-space:pre-wrap;
}
.toast{
  position:fixed;left:50%;bottom:26px;transform:translateX(-50%);
  background:var(--panel);border:1px solid var(--line);box-shadow:var(--shadow);
  border-radius:var(--radius-sm);padding:10px 15px;font-size:13.5px;z-index:90;
  animation:rise .3s cubic-bezier(.2,.7,.3,1) both;max-width:min(520px,92vw);
}
.toast.bad{border-color:var(--accent)}
</style>
</head>
<body>

<header>
  <div class="bar">
    <div class="brand"><i class="dot"></i>clips<span>valorant</span></div>

    <form class="search" id="search" autocomplete="off">
      <label class="field">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/>
          <path d="m20 20-3.2-3.2"/></svg>
        <input id="riot" placeholder="Name#TAG" spellcheck="false" required>
      </label>
      <label class="field" title="How far back to look">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/>
          <path d="M12 7v5l3 2"/></svg>
        <select id="days"></select>
      </label>
      <select class="field" id="acct" title="Stored accounts"></select>
      <button class="btn" id="go" type="submit">Search</button>
    </form>

    <button class="icon-btn" id="theme" title="Light or dark" aria-label="Toggle theme">
      <svg class="sun" width="16" height="16" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2.1" stroke-linecap="round">
        <circle cx="12" cy="12" r="4.2"/>
        <path d="M12 2.6v2M12 19.4v2M2.6 12h2M19.4 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M18.7 5.3l-1.4 1.4M6.7 17.3l-1.4 1.4"/>
      </svg>
      <svg class="moon" width="16" height="16" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2.1" stroke-linejoin="round">
        <path d="M20 14.2A8.4 8.4 0 0 1 9.8 4 8.4 8.4 0 1 0 20 14.2Z"/>
      </svg>
    </button>
  </div>

  <div class="rail" id="rail"><div class="fill" id="fill"></div></div>

  <div class="status" id="status">
    <i class="spark"></i>
    <span class="msg" id="statusMsg"></span>
    <span class="count" id="statusCount"></span>
    <button class="linkish" id="logToggle" type="button">log</button>
  </div>
</header>

<main>
  <div class="split">
    <section class="panel">
      <div class="rowhead">
        <h2>Found</h2>
        <span class="pill" id="foundCount">0</span>
      </div>
      <div class="finds" id="finds"></div>
    </section>
    <section id="stageWrap"></section>
  </div>
  <pre class="log" id="log" hidden></pre>
</main>

<script>
(() => {
"use strict";

/* ---------- theme ------------------------------------------------- */
const html = document.documentElement;
const THEME_KEY = "cv-theme";
function initTheme(){
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (e) {}
  if (saved !== "light" && saved !== "dark") {
    saved = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  html.dataset.theme = saved;
}
initTheme();
document.getElementById("theme").addEventListener("click", () => {
  const next = html.dataset.theme === "dark" ? "light" : "dark";
  html.dataset.theme = next;
  try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
});

/* ---------- helpers ----------------------------------------------- */
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const pad2 = (n) => String(n).padStart(2, "0");
function hhmmss(s){
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), x = s%60;
  return h ? h + ":" + pad2(m) + ":" + pad2(x) : m + ":" + pad2(x);
}
function stampOf(s){
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), x = s%60;
  return pad2(h) + "h" + pad2(m) + "m" + pad2(x) + "s";
}
function ago(epoch){
  const d = Date.now()/1000 - epoch;
  if (d < 3600) return Math.max(1, Math.round(d/60)) + "m ago";
  if (d < 86400) return Math.round(d/3600) + "h ago";
  return Math.round(d/86400) + "d ago";
}
let toastTimer = null;
function toast(text, bad){
  const old = document.querySelector(".toast");
  if (old) old.remove();
  const n = el("div", "toast" + (bad ? " bad" : ""), text);
  document.body.appendChild(n);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => n.remove(), 6500);
}
async function api(path, opts){
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

/* ---------- the Twitch player ------------------------------------- */
/* The embed script gives real seeking. Without it every jump would be an
   iframe reload, which restarts the video and throws away the buffer - so
   the script is tried first and the iframe is the fallback, not the plan. */
const Player = (() => {
  const host = location.hostname;
  let api = null, ready = false, vod = null;
  let script = "idle";      // idle | loading | ok | failed
  let pending = null;

  function flush(){
    if (!pending) return;
    const p = pending; pending = null;
    open(p.vod, p.seconds);
  }
  (function load(){
    script = "loading";
    const s = document.createElement("script");
    s.src = "https://embed.twitch.tv/embed/v1.js";
    s.async = true;
    s.onload = () => { script = "ok"; flush(); };
    s.onerror = () => { script = "failed"; flush(); };
    document.head.appendChild(s);
  })();

  function stage(){
    const node = document.querySelector(".stage");
    if (node) node.innerHTML = "";
    return node;
  }

  function fallback(vodId, seconds){
    const box = stage();
    if (!box) return;
    const f = document.createElement("iframe");
    f.allowFullscreen = true;
    f.src = "https://player.twitch.tv/?video=v" + encodeURIComponent(vodId) +
            "&time=" + stampOf(seconds) +
            "&parent=" + encodeURIComponent(host) + "&autoplay=true";
    box.appendChild(f);
    api = null; ready = false; vod = vodId;
  }

  function open(vodId, seconds){
    if (script === "loading") { pending = {vod: vodId, seconds: seconds}; return; }
    if (script !== "ok" || !window.Twitch || !window.Twitch.Player) {
      fallback(vodId, seconds); return;
    }
    if (api && vod === vodId && ready) { try { api.seek(seconds); return; } catch (e) {} }
    const box = stage();
    if (!box) return;
    const mount = document.createElement("div");
    mount.id = "tw-" + Date.now();
    box.appendChild(mount);
    ready = false; vod = vodId;
    api = new window.Twitch.Player(mount.id, {
      video: String(vodId), parent: [host],
      width: "100%", height: "100%",
      autoplay: true, muted: false, time: stampOf(seconds),
    });
    api.addEventListener(window.Twitch.Player.READY, () => { ready = true; });
  }

  return {
    play(vodId, seconds){ open(vodId, Math.max(0, Math.round(seconds))); },
    time(){
      if (!api || !ready) return null;
      try { const t = api.getCurrentTime(); return typeof t === "number" ? t : null; }
      catch (e) { return null; }
    },
    reset(){ api = null; ready = false; vod = null; },
  };
})();

/* ---------- state -------------------------------------------------- */
const S = {
  key: "", groups: [], current: null, tab: "best",
  defaults: {}, jobTimer: null, headTimer: null,
};

/* ---------- search bar --------------------------------------------- */
function fillDays(def, max){
  const sel = $("days");
  sel.innerHTML = "";
  [3, 7, 14, 30].filter((d) => d <= (max || 30)).forEach((d) => {
    const o = el("option", null, d + " days");
    o.value = String(d);
    if (d === def) o.selected = true;
    sel.appendChild(o);
  });
}

function fillAccounts(list, prefer){
  const sel = $("acct");
  sel.innerHTML = "";
  if (!list.length) { sel.hidden = true; return; }
  sel.hidden = false;
  const seen = {};
  list.forEach((a) => { seen[a.riot_id] = (seen[a.riot_id] || 0) + 1; });
  list.forEach((a) => {
    const o = el("option", null,
      a.riot_id + (seen[a.riot_id] > 1 ? "  (" + a.key + ")" : ""));
    o.value = a.key;
    sel.appendChild(o);
  });
  const has = (k) => list.some((a) => a.key === k);
  sel.value = (prefer && has(prefer)) ? prefer
            : (has(S.key) ? S.key : list[0].key);
}

$("acct").addEventListener("change", (e) => loadClips(e.target.value));

$("search").addEventListener("submit", async (e) => {
  e.preventDefault();
  const riot_id = $("riot").value.trim();
  if (!riot_id) return;
  $("go").disabled = true;
  try {
    const job = await api("/api/search", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({riot_id: riot_id, days: Number($("days").value)}),
    });
    showJob(job);
    pollJob();
  } catch (err) {
    $("go").disabled = false;
    toast(err.message, true);
  }
});

/* ---------- job progress ------------------------------------------- */
function showJob(job){
  const running = job && job.state === "running";
  $("status").classList.toggle("on", !!job && job.state !== "idle");
  $("go").disabled = !!running;

  if (!job || job.state === "idle") {
    $("rail").classList.remove("indet");
    $("fill").style.width = "0";
    return;
  }

  const lines = job.lines || [];
  $("statusMsg").textContent = job.state === "error"
    ? ("Failed - " + job.error)
    : (lines.length ? lines[lines.length - 1] : "Starting...");
  $("log").textContent = lines.join("\n");

  if (job.phase === "resolve" && job.total > 0) {
    $("rail").classList.remove("indet");
    $("fill").style.width = (100 * clamp(job.done / job.total, 0, 1)).toFixed(1) + "%";
    $("statusCount").textContent = job.done + " / " + job.total + " matches";
  } else if (running) {
    $("rail").classList.add("indet");
    $("statusCount").textContent = job.elapsed ? Math.round(job.elapsed) + "s" : "";
  } else {
    $("rail").classList.remove("indet");
    $("fill").style.width = "100%";
    $("statusCount").textContent = job.elapsed ? Math.round(job.elapsed) + "s" : "";
  }
}

function pollJob(){
  clearInterval(S.jobTimer);
  S.jobTimer = setInterval(async () => {
    let job;
    try { job = await api("/api/job"); } catch (e) { return; }
    showJob(job);
    if (job.state === "running") return;
    clearInterval(S.jobTimer);
    setTimeout(() => {
      $("status").classList.remove("on");
      $("fill").style.width = "0";
    }, 2600);
    await refresh(job.key);
    if (job.state === "error") toast(job.error, true);
  }, 900);
}

$("logToggle").addEventListener("click", () => { $("log").hidden = !$("log").hidden; });

/* ---------- the find list ------------------------------------------ */
function renderFinds(){
  const box = $("finds");
  box.innerHTML = "";
  $("foundCount").textContent = String(S.groups.length);

  S.groups.forEach((g, i) => {
    const b = el("button", "find");
    b.type = "button";
    b.style.animationDelay = Math.min(i * 35, 420) + "ms";
    b.setAttribute("aria-current", S.current && S.current.id === g.id ? "true" : "false");

    const top = el("div", "top");
    top.appendChild(el("span", "who", g.channel));
    top.appendChild(el("span", "tag " + g.verdict, g.verdict));
    b.appendChild(top);

    const sub = el("div", "sub");
    sub.appendChild(el("b", null, g.map));
    sub.appendChild(el("span", null, "·"));
    sub.appendChild(el("span", null, ago(g.started)));
    sub.appendChild(el("span", null, "·"));
    sub.appendChild(el("span", null,
      g.playable + (g.playable === 1 ? " highlight" : " highlights")));
    b.appendChild(sub);

    b.addEventListener("click", () => select(g));
    box.appendChild(b);
  });
}

function emptyState(){
  const wrap = el("section", "panel");
  const box = el("div", "empty");
  const art = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  art.setAttribute("width", "34"); art.setAttribute("height", "34");
  art.setAttribute("viewBox", "0 0 24 24"); art.setAttribute("fill", "none");
  art.setAttribute("stroke", "currentColor"); art.setAttribute("stroke-width", "1.6");
  art.setAttribute("stroke-linejoin", "round");
  art.innerHTML = '<rect x="2.5" y="5" width="19" height="14" rx="3"/>' +
                  '<path d="m10 9.5 5 2.5-5 2.5Z"/>';
  box.appendChild(art);
  box.appendChild(el("div", "big", "Nothing to watch yet"));
  box.appendChild(el("p", null,
    "Search a Riot ID to look through the last " +
    (S.defaults.default_days || 14) + " days. Twitch deletes broadcasts after " +
    "one or two weeks, so anything older has nothing left to link to."));
  wrap.appendChild(box);
  return wrap;
}

/* ---------- the theatre -------------------------------------------- */
function allKills(g){
  const out = [];
  (g.rounds || []).forEach((r) => (r.kills || []).forEach((k) => out.push(k)));
  out.sort((a, b) => a.at - b.at);
  return out;
}

function position(g, at){
  const a = g.span.start, b = g.span.end;
  return clamp(((at - a) / Math.max(1, b - a)) * 100, 0, 100);
}

function select(g){
  S.current = g;
  S.tab = "best";
  renderFinds();
  renderTheatre();
  const first = (g.clips || []).find((c) => c.playable)
             || (g.rounds || []).find((r) => r.playable);
  if (first) jump(first);
}

function jump(m){
  if (!S.current) return;
  if (!m || !m.playable) { if (m && m.why_not) toast(m.why_not); return; }
  Player.play(S.current.vod_id, m.offset);
  document.querySelectorAll(".moment").forEach((n) =>
    n.setAttribute("aria-current", "false"));
  if (m._node) m._node.setAttribute("aria-current", "true");
  moveHead(m.at);
}

function moveHead(at){
  if (!S.current) return;
  const head = document.querySelector(".scrub .head");
  const played = document.querySelector(".scrub .played");
  if (!head) return;
  const pct = position(S.current, at);
  head.style.left = pct + "%";
  if (played) played.style.width = pct + "%";
}

function renderTheatre(){
  const wrap = $("stageWrap");
  wrap.innerHTML = "";
  const g = S.current;
  if (!g) { wrap.appendChild(emptyState()); clearInterval(S.headTimer); return; }

  Player.reset();
  const panel = el("section", "panel theatre");

  const stage = el("div", "stage");
  stage.appendChild(el("div", "placeholder", "Pick a moment to start playing."));
  panel.appendChild(stage);

  const meta = el("div", "meta");
  meta.appendChild(el("span", "title", g.channel));
  meta.appendChild(el("span", "sep", "/"));
  meta.appendChild(el("span", "dim", g.riot_id));
  meta.appendChild(el("span", "tag " + (g.same_team ? "team" : "confirmed"),
    g.same_team ? "your team" : "enemy"));
  const right = el("div", "right");
  right.appendChild(el("span", "dim",
    g.map + (g.score ? " · " + g.score : "") + " · " + ago(g.started)));
  if (g.timing !== "measured") {
    const warn = el("span", "tag likely", "timing estimated");
    warn.title = "No round countdown could be read off this broadcast, so the " +
                 "offset is the median of the measured ones. Expect it to be a " +
                 "few seconds out.";
    right.appendChild(warn);
  }
  const open = el("a", "linkish", "on twitch");
  open.href = "https://www.twitch.tv/videos/" + g.vod_id;
  open.target = "_blank"; open.rel = "noopener noreferrer";
  right.appendChild(open);
  meta.appendChild(right);
  panel.appendChild(meta);

  panel.appendChild(buildScrub(g));

  const tabrow = el("div", "meta");
  tabrow.style.paddingTop = "0";
  const tabs = el("div", "tabs");
  const defs = [
    ["best", "Highlights", (g.clips || []).length],
    ["rounds", "Rounds", (g.rounds || []).length],
    ["kills", "Kills", g.kill_count || 0],
  ];
  defs.forEach((def) => {
    const t = el("button", null, def[1] + (def[2] ? "  " + def[2] : ""));
    t.type = "button";
    t.setAttribute("aria-selected", S.tab === def[0] ? "true" : "false");
    t.addEventListener("click", () => {
      S.tab = def[0];
      Array.prototype.forEach.call(tabs.children, (b, i) =>
        b.setAttribute("aria-selected", defs[i][0] === S.tab ? "true" : "false"));
      renderMoments(panel, g);
    });
    tabs.appendChild(t);
  });
  tabrow.appendChild(tabs);
  panel.appendChild(tabrow);

  const list = el("div", "moments");
  list.id = "moments";
  panel.appendChild(list);

  if ((g.reasons || []).length) {
    const d = document.createElement("details");
    d.className = "why";
    d.appendChild(el("summary", null, "Why this is " + g.verdict));
    const ul = document.createElement("ul");
    g.reasons.forEach((r) => ul.appendChild(el("li", null, r)));
    d.appendChild(ul);
    panel.appendChild(d);
  }

  wrap.appendChild(panel);
  renderMoments(panel, g);
  startHeadTimer();
}

function buildScrub(g){
  const wrap = el("div", "scrubwrap");
  const scrub = el("div", "scrub");
  scrub.appendChild(el("div", "track"));
  scrub.appendChild(el("div", "played"));

  (g.rounds || []).forEach((r) => {
    const t = el("i", "tick");
    t.style.left = position(g, r.at) + "%";
    scrub.appendChild(t);
  });

  allKills(g).forEach((k) => {
    const m = el("button", "mark kill" + (k.playable ? "" : " out"));
    m.type = "button";
    m.style.left = position(g, k.at) + "%";
    m.title = "Round " + (k.round + 1) + " · " + (k.victim || "kill") +
              (k.weapon ? " · " + k.weapon : "");
    m.appendChild(el("i"));
    m.addEventListener("click", (e) => { e.stopPropagation(); jump(k); });
    scrub.appendChild(m);
  });

  (g.rounds || []).forEach((r) => {
    if (r.died == null) return;
    const m = el("span", "mark death");
    m.style.left = position(g, r.died) + "%";
    m.title = "You died · round " + (r.round + 1);
    m.appendChild(el("i"));
    scrub.appendChild(m);
  });

  (g.clips || []).forEach((c) => {
    const m = el("button", "mark star" + (c.playable ? "" : " out"));
    m.type = "button";
    m.style.left = position(g, c.at) + "%";
    m.title = c.label + " · round " + (c.round + 1);
    m.appendChild(el("i"));
    m.addEventListener("click", (e) => { e.stopPropagation(); jump(c); });
    scrub.appendChild(m);
  });

  scrub.appendChild(el("i", "head"));

  /* Click anywhere on the rail to go there. This is the point of the
     scrubber: the highlights are suggestions, not the only way in. */
  scrub.addEventListener("click", (e) => {
    const box = scrub.getBoundingClientRect();
    const frac = clamp((e.clientX - box.left) / box.width, 0, 1);
    const at = Math.round(g.span.start + frac * (g.span.end - g.span.start));
    if (at < 0) { toast("That is before this broadcast started."); return; }
    if (g.vod_duration && at > g.vod_duration) {
      toast("That is past the end of this broadcast."); return;
    }
    Player.play(g.vod_id, Math.max(0, at - 4));
    moveHead(at);
  });

  wrap.appendChild(scrub);
  const ends = el("div", "ends");
  ends.appendChild(el("span", null, "round 1"));
  ends.appendChild(el("span", null, hhmmss(g.span.end - g.span.start) + " of match"));
  wrap.appendChild(ends);
  return wrap;
}

function renderMoments(panel, g){
  const list = panel.querySelector("#moments");
  list.innerHTML = "";

  const row = (m, label, extra, badge) => {
    const b = el("button", "moment" + (m.playable ? "" : " out"));
    b.type = "button";
    b.setAttribute("aria-current", "false");
    b.appendChild(el("span", "stamp", hhmmss(m.offset)));
    const what = el("span", "what");
    what.appendChild(el("b", null, label));
    if (extra) {
      what.appendChild(document.createTextNode("  "));
      what.appendChild(el("em", null, extra));
    }
    b.appendChild(what);
    if (badge) b.appendChild(el("span", "badge", badge));
    if (!m.playable) b.title = m.why_not;
    m._node = b;
    b.addEventListener("click", () => jump(m));
    list.appendChild(b);
  };

  if (S.tab === "best") {
    const clips = g.clips || [];
    if (!clips.length) {
      list.appendChild(el("div", "empty",
        "No multi-kills in this match. Every round is under Rounds."));
      return;
    }
    clips.forEach((c) => row(c, c.label, "round " + (c.round + 1),
      c.won === true ? "won" : (c.won === false ? "lost" : "")));
  } else if (S.tab === "rounds") {
    const rounds = g.rounds || [];
    if (!rounds.length) {
      list.appendChild(el("div", "empty", "No rounds stored for this match."));
      return;
    }
    rounds.forEach((r) => {
      const n = (r.kills || []).length;
      row(r, "Round " + (r.round + 1),
        n ? (n + (n === 1 ? " kill" : " kills")) : "no kills of yours",
        r.died != null ? "died" : "");
    });
  } else {
    const kills = allKills(g);
    if (!kills.length) {
      list.appendChild(el("div", "empty", "No kills recorded for you in this match."));
      return;
    }
    let lastRound = -1;
    kills.forEach((k) => {
      if (k.round !== lastRound) {
        lastRound = k.round;
        const h = el("div", "roundhead");
        h.appendChild(el("span", null, "Round " + (k.round + 1)));
        h.appendChild(el("i", "line"));
        list.appendChild(h);
      }
      row(k, k.victim || "kill", k.weapon || "", "");
    });
  }
}

/* The playhead follows the real player when the embed API is available, so
   the scrubber shows where you are rather than where you last clicked. */
function startHeadTimer(){
  clearInterval(S.headTimer);
  S.headTimer = setInterval(() => {
    const t = Player.time();
    if (t != null && S.current) moveHead(t);
  }, 1000);
}

/* ---------- keyboard ------------------------------------------------ */
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea")) return;
  if (!S.current) return;
  const list = Array.prototype.filter.call(
    document.querySelectorAll(".moment"), (n) => !n.classList.contains("out"));
  if (!list.length) return;
  const at = list.findIndex((n) => n.getAttribute("aria-current") === "true");
  if (e.key === "ArrowDown" || e.key === "j") {
    e.preventDefault(); list[clamp(at + 1, 0, list.length - 1)].click();
  } else if (e.key === "ArrowUp" || e.key === "k") {
    e.preventDefault(); list[clamp(at < 0 ? 1 : at - 1, 0, list.length - 1)].click();
  }
});

/* ---------- loading ------------------------------------------------- */
async function loadClips(key){
  let data;
  try { data = await api("/api/clips?key=" + encodeURIComponent(key || "")); }
  catch (err) { toast(err.message, true); return; }
  S.key = data.key;
  S.groups = data.groups || [];
  S.current = (S.current && S.groups.find((g) => g.id === S.current.id)) || null;
  renderFinds();
  renderTheatre();
}

async function refresh(prefer){
  let state;
  try { state = await api("/api/state"); }
  catch (err) { toast(err.message, true); return; }
  S.defaults = state;
  if (!$("riot").value) $("riot").value = state.default_riot_id || "";
  if (!$("days").options.length) fillDays(state.default_days || 14, state.max_days || 30);
  fillAccounts(state.accounts || [], prefer);
  if (!state.has_twitch) {
    toast("Twitch credentials are missing, so a search stops after the match " +
          "history. Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env.", true);
  }
  await loadClips($("acct").value || state.default_key);
  if (state.job) {
    showJob(state.job);
    if (state.job.state === "running") pollJob();
  }
}

refresh();
})();
</script>
</body>
</html>
"""
