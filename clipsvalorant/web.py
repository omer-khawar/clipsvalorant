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
        cutoff = int(time.time()) - days * 86400

        job.step("ingest")
        job.say(f"Looking up {acct.riot_id} in {acct.region.upper()}...")
        # The window is handed to ingest, not applied after it. A match
        # outside it is never going to be resolved, and its detail call is
        # the most expensive thing in the whole run.
        report = ingest(acct, limit=fetch, mode=mode, since_epoch=cutoff)
        job.say(report.summary())
        for err in report.errors[:5]:
            job.say(f"! {err}")
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
/* ===========================================================================
   Pulled from github.com/wundercorp/awesome-components. That catalogue ships
   rendered DOM snapshots rather than source, so what is taken here is the
   measured thing: exact spacing, radii, border alphas, type scale and motion
   timing, reimplemented in plain CSS.

     shell, dot-grid, rules, card system   reapollo / bento-monochrome
     metric tiles                          reapollo / glassmorphism-minimal-metrics-block
     theme control                         ncdai   / theme-switcher-1
     segmented controls                    ruixenui/ segmented-button-group
     scrubber ticks + chapter jumps        ruixenui/ audio-timeline-with-chapters
     search field                          kokonutd/ action-search-bar
     scan progress                         educalvolpz / animated-progress-bar

   The background shader is written here - the catalogue has no shader source
   anywhere in it - and follows the monochrome silk of reuno-ui's
   background-paper-shaders.
   =========================================================================== */

:root{
  color-scheme:light;
  --bg:#ffffff;
  --ink:#0a0a0a;
  --muted:rgba(10,10,10,.58);
  --faint:rgba(10,10,10,.36);
  --line:rgba(10,10,10,.10);
  --hair:rgba(10,10,10,.06);
  --panel:rgba(255,255,255,.72);
  --solid:#ffffff;
  --grid:rgba(17,17,17,.075);
  --edge:#ffffff;
  --accent:#6b2fd4;
  --on-accent:#ffffff;
  --win:#0a0a0a;
  --shadow:0 10px 40px rgba(0,0,0,.045);
  --shadow-h:0 20px 60px rgba(0,0,0,.085);
  --shader:.5;
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --bg:#050505;
  --ink:#fafafa;
  --muted:rgba(255,255,255,.58);
  --faint:rgba(255,255,255,.36);
  --line:rgba(255,255,255,.11);
  --hair:rgba(255,255,255,.06);
  --panel:rgba(255,255,255,.045);
  --solid:#0d0d0d;
  --grid:rgba(255,255,255,.06);
  --edge:#000000;
  --accent:#a97bff;
  --on-accent:#0a0a0a;
  --win:#fafafa;
  --shadow:0 18px 40px rgba(0,0,0,.35);
  --shadow-h:0 28px 70px rgba(0,0,0,.55);
  --shader:.72;
}

*,*::before,*::after{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0;
  background:var(--bg);
  color:var(--ink);
  font:400 15px/1.5 ui-sans-serif,-apple-system,"Segoe UI Variable Text","Segoe UI",Inter,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;
  font-variant-numeric:tabular-nums;
  overflow-x:hidden;
}
.mono{font-family:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}

/* ---- background: shader, then the 20px dot-grid, then an edge vignette ---- */
#shader{position:fixed;inset:0;width:100%;height:100%;z-index:-30;opacity:var(--shader);
  transition:opacity .5s ease;display:block}
.dotgrid{
  position:fixed;inset:0;z-index:-20;pointer-events:none;opacity:.9;
  background-image:linear-gradient(to right,var(--grid) 1px,transparent 1px),
                   linear-gradient(to bottom,var(--grid) 1px,transparent 1px);
  background-size:20px 20px;
  -webkit-mask-image:repeating-linear-gradient(to right,#000 0 3px,transparent 3px 8px),
        repeating-linear-gradient(#000 0 3px,transparent 3px 8px);
  mask-image:repeating-linear-gradient(to right,#000 0 3px,transparent 3px 8px),
        repeating-linear-gradient(#000 0 3px,transparent 3px 8px);
  -webkit-mask-composite:source-in;mask-composite:intersect;
}
.vignette{position:fixed;inset:0;z-index:-10;pointer-events:none;opacity:.75;filter:blur(40px);
  background:radial-gradient(circle at center,rgba(0,0,0,0) 55%,var(--edge) 100%)}

/* ---- shell ---- */
.wrap{max-width:1152px;margin:0 auto;padding:56px 24px 72px;position:relative}
.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.35em;color:var(--faint);
  display:block;line-height:1}
.rule{height:1px;background:var(--line);border:0;margin:0}

.top{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;
  padding-bottom:24px;border-bottom:1px solid var(--line);margin-bottom:28px}
.brand h1{margin:10px 0 0;font-size:40px;line-height:1.02;letter-spacing:-.03em;font-weight:800}
.brand h1 .tag{color:var(--faint);font-weight:500}
.topright{display:flex;align-items:center;gap:10px}
.chip{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 12px;border-radius:999px;
  border:1px solid var(--line);color:var(--muted);font-size:10px;text-transform:uppercase;
  letter-spacing:.28em;background:var(--panel);backdrop-filter:blur(12px)}

/* ---- theme switcher: ncdai/theme-switcher-1 ---- */
.themer{display:inline-flex;align-items:center;border-radius:999px;background:var(--panel);
  box-shadow:inset 0 0 0 1px var(--line);backdrop-filter:blur(12px);overflow:hidden}
.themer button{position:relative;width:32px;height:32px;display:grid;place-items:center;border:0;
  background:none;color:var(--faint);cursor:pointer;transition:color .2s}
.themer button:hover{color:var(--ink)}
.themer button[aria-checked="true"]{color:var(--ink)}
.themer button[aria-checked="true"]::after{content:"";position:absolute;inset:0;border-radius:999px;
  border:1px solid var(--line)}
.themer svg{width:15px;height:15px}

/* ---- search: kokonutd/action-search-bar ---- */
.scan{margin-bottom:28px}
.scanrow{display:grid;grid-template-columns:1fr auto auto;gap:12px;align-items:end}
.field{display:flex;flex-direction:column;gap:8px;min-width:0}
.lbl{font-size:10px;text-transform:uppercase;letter-spacing:.28em;color:var(--faint);line-height:1}
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
.seg button[aria-checked="true"],.seg button[aria-selected="true"]{
  background:var(--ink);color:var(--bg)}
.seg.sm{height:32px;padding:3px}
.seg.sm button{font-size:11px;padding:0 12px;text-transform:uppercase;letter-spacing:.1em}

.go{height:44px;padding:0 22px;border-radius:999px;border:0;background:var(--accent);
  color:var(--on-accent);font:inherit;font-weight:600;font-size:14px;cursor:pointer;
  display:inline-flex;align-items:center;gap:8px;transition:transform .2s,filter .2s}
.go:hover:not(:disabled){filter:brightness(1.08);transform:translateY(-1px)}
.go:disabled{opacity:.5;cursor:default}
.go svg{width:15px;height:15px}

/* ---- progress: educalvolpz/animated-progress-bar ---- */
.prog{margin-top:16px}
.prog[hidden]{display:none}
.pmeta{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;
  font-size:11px;text-transform:uppercase;letter-spacing:.24em;color:var(--muted)}
.ptrack{position:relative;height:6px;border-radius:999px;overflow:hidden;background:var(--hair);
  box-shadow:inset 0 0 0 1px var(--line)}
.pfill{height:100%;width:0;border-radius:999px;background:var(--ink);transition:width .45s cubic-bezier(.22,1,.36,1)}
.pfill.indet{width:35%;animation:slide 1.4s ease-in-out infinite}
@keyframes slide{0%{transform:translateX(-110%)}100%{transform:translateX(330%)}}
.logbtn{margin-top:10px;border:0;background:none;color:var(--faint);font:inherit;font-size:11px;
  text-transform:uppercase;letter-spacing:.24em;cursor:pointer;padding:0}
.logbtn:hover{color:var(--ink)}
.log{margin:10px 0 0;max-height:190px;overflow:auto;font-family:ui-monospace,"Cascadia Mono",Menlo,Consolas,monospace;
  font-size:11.5px;line-height:1.7;color:var(--muted);white-space:pre-wrap;
  border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:var(--panel)}

/* ---- cards: reapollo/bento-monochrome ---- */
.card{position:relative;border:1px solid var(--line);border-radius:16px;background:var(--panel);
  backdrop-filter:blur(16px);box-shadow:var(--shadow)}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}
.rise{opacity:0;animation:rise .8s cubic-bezier(.22,1,.36,1) forwards}
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}

/* ---- metric tiles: reapollo/glassmorphism-minimal-metrics-block ---- */
.stats{margin-bottom:12px}
.tile{grid-column:span 3;border-radius:16px;border:1px solid var(--line);background:var(--panel);
  backdrop-filter:blur(16px);padding:20px;box-shadow:var(--shadow);
  transition:transform .3s ease,box-shadow .3s ease}
.tile:hover{transform:translateY(-3px);box-shadow:var(--shadow-h)}
.tile .k{font-size:10px;text-transform:uppercase;letter-spacing:.25em;color:var(--faint)}
.tile .v{margin-top:14px;font-size:38px;line-height:1;letter-spacing:-.035em;font-weight:700}
.tile .v small{font-size:15px;font-weight:500;color:var(--faint);letter-spacing:0;margin-left:3px}
.tile .s{margin-top:8px;font-size:12.5px;color:var(--muted)}

/* ---- bento body ---- */
.body{margin-top:12px}
.finds{grid-column:span 4;display:flex;flex-direction:column;min-height:420px;overflow:hidden}
.stage{grid-column:span 8;display:flex;flex-direction:column;overflow:hidden}
.head{display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:14px 16px;border-bottom:1px solid var(--hair);min-height:58px}
.head .t{font-size:11px;text-transform:uppercase;letter-spacing:.28em;color:var(--faint)}

.findlist{list-style:none;margin:0;padding:6px;overflow:auto;flex:1}
.find{width:100%;text-align:left;border:1px solid transparent;background:none;cursor:pointer;
  border-radius:12px;padding:11px 12px;display:grid;gap:5px;color:inherit;font:inherit;
  transition:background .18s,border-color .18s}
.find:hover{background:var(--hair)}
.find[aria-current="true"]{background:var(--hair);border-color:var(--line)}
.find .r1{display:flex;align-items:center;gap:8px}
.find .ch{font-weight:650;font-size:14px;letter-spacing:-.01em;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.find .r2{display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--muted)}
.find .dot{width:4px;height:4px;border-radius:50%;background:var(--faint);flex:none}
.badge{flex:none;font-size:9px;text-transform:uppercase;letter-spacing:.2em;padding:3px 7px;
  border-radius:999px;border:1px solid var(--line);color:var(--muted);line-height:1}
.badge.confirmed{border-color:color-mix(in srgb,var(--accent) 50%,transparent);
  color:var(--accent)}
.count{margin-left:auto;flex:none;font-size:11px;color:var(--faint)}

/* ---- player + scrubber ---- */
.player{position:relative;aspect-ratio:16/9;background:#000;border-bottom:1px solid var(--hair)}
.player iframe,.player>div{position:absolute;inset:0;width:100%;height:100%;border:0}
.empty{flex:1;display:grid;place-items:center;padding:56px 24px;text-align:center;color:var(--faint)}
.empty p{margin:10px 0 0;font-size:13px;max-width:34ch;color:var(--muted)}

.deck{padding:14px 16px 16px}
.nowline{display:flex;align-items:baseline;gap:10px;margin-bottom:12px;min-height:20px}
.nowline .n{font-size:13px;font-weight:600;letter-spacing:-.01em}
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
  padding:5px 9px;font-size:11.5px;box-shadow:var(--shadow-h);opacity:0;transition:opacity .15s;z-index:4}
.tip.on{opacity:1}
.tip b{font-weight:650}
.tip span{color:var(--faint);margin-left:6px}

.axis{display:flex;justify-content:space-between;margin-top:4px;font-size:10.5px;color:var(--faint)}

/* chapter jumps */
.chapters{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px;padding-top:14px;
  border-top:1px solid var(--hair)}
.chap{height:28px;min-width:30px;padding:0 9px;border-radius:8px;border:1px solid var(--line);
  background:none;color:var(--muted);font:inherit;font-size:11.5px;cursor:pointer;
  transition:background .16s,color .16s,border-color .16s}
.chap:hover{background:var(--hair);color:var(--ink)}
.chap[aria-current="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.chap:disabled{opacity:.35;cursor:default}

.moments{list-style:none;margin:14px 0 0;padding:14px 0 0;border-top:1px solid var(--hair);
  display:grid;gap:2px;max-height:230px;overflow:auto}
.moment{display:flex;align-items:center;gap:10px;width:100%;border:0;background:none;color:inherit;
  font:inherit;text-align:left;padding:7px 8px;border-radius:9px;cursor:pointer;transition:background .16s}
.moment:hover{background:var(--hair)}
.moment .lab{font-size:13px;font-weight:550;letter-spacing:-.01em}
.moment .sub{font-size:11.5px;color:var(--faint)}
.moment .st{margin-left:auto;font-size:11.5px;color:var(--muted)}
.moment .st.no{color:var(--faint);text-decoration:line-through}
.star-ico{width:11px;height:11px;flex:none;color:var(--accent)}

.note{padding:12px 16px;font-size:12px;color:var(--muted);border-top:1px solid var(--hair)}
.foot{margin-top:28px;padding-top:20px;border-top:1px solid var(--line);
  font-size:10px;text-transform:uppercase;letter-spacing:.3em;color:var(--faint);
  display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}
.err{margin-top:12px;border:1px solid color-mix(in srgb,#e5484d 45%,transparent);border-radius:12px;
  padding:11px 14px;font-size:13px;color:#e5484d;background:color-mix(in srgb,#e5484d 7%,transparent)}
.err[hidden]{display:none}

::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:999px}
::-webkit-scrollbar-track{background:transparent}

@media (max-width:900px){
  .wrap{padding:36px 18px 56px}
  .brand h1{font-size:30px}
  .scanrow{grid-template-columns:1fr}
  .tile{grid-column:span 6}
  .finds,.stage{grid-column:span 12}
}
@media (prefers-reduced-motion:reduce){
  *{animation-duration:.001ms !important;transition-duration:.001ms !important}
  .rise{opacity:1}
}
::view-transition-old(root),::view-transition-new(root){animation:none;mix-blend-mode:normal}
::view-transition-old(root){z-index:0}
::view-transition-new(root){z-index:1}
</style>
</head>
<body>
<canvas id="shader" aria-hidden="true"></canvas>
<div class="dotgrid" aria-hidden="true"></div>
<div class="vignette" aria-hidden="true"></div>

<main class="wrap">
  <header class="top">
    <div class="brand">
      <span class="eyebrow">clipsvalorant</span>
      <h1 id="who">&mdash;</h1>
    </div>
    <div class="topright">
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
      <button class="go" id="go" type="submit">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
        Scan
      </button>
    </form>

    <div class="prog" id="prog" hidden>
      <div class="pmeta"><span id="pphase">Starting</span><span id="ppct" class="mono"></span></div>
      <div class="ptrack"><div class="pfill" id="pfill"></div></div>
      <button class="logbtn" id="logbtn" type="button" aria-expanded="false">Show log</button>
      <pre class="log" id="log" hidden></pre>
    </div>
    <div class="err" id="err" hidden></div>
  </section>

  <section class="grid stats" id="stats"></section>

  <section class="grid body">
    <aside class="card finds">
      <div class="head">
        <span class="t">Finds</span>
        <div class="seg sm" role="tablist" aria-label="Filter finds" id="filter">
          <button type="button" role="tab" data-f="all" aria-selected="true">All</button>
          <button type="button" role="tab" data-f="confirmed" aria-selected="false">Confirmed</button>
        </div>
      </div>
      <ul class="findlist" id="findlist"></ul>
    </aside>

    <section class="card stage" id="stage"></section>
  </section>

  <footer class="foot">
    <span id="footleft">Timestamps measured against each broadcast</span>
    <span id="footright"></span>
  </footer>
</main>

<script>
/* =====================================================================
   background shader - monochrome domain-warped flow.
   Nothing in awesome-components ships shader source, so this is written
   here; the look follows reuno-ui/background-paper-shaders: greyscale
   silk, very low contrast, slow.
   ===================================================================== */
(function shader(){
  var cv = document.getElementById("shader");
  var gl = null;
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
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) { return null; }
    return s;
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

  var uRes = gl.getUniformLocation(pr, "u_res"),
      uT   = gl.getUniformLocation(pr, "u_t"),
      uD   = gl.getUniformLocation(pr, "u_dark"),
      uM   = gl.getUniformLocation(pr, "u_m");

  var mx = 0, my = 0, tx = 0, ty = 0;
  addEventListener("pointermove", function(e){
    tx = (e.clientX / innerWidth - 0.5) * 2;
    ty = (0.5 - e.clientY / innerHeight) * 2;
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
    if (document.hidden) { return; }
    if (now - last < 33) { return; }          /* ~30fps is plenty for this */
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

var S = {
  key: "", groups: [], sel: null, filter: "all", days: 14,
  player: null, poll: null, tick: null, hasTwitch: true
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
  var vt = document.startViewTransition(function(){ paint(pref); });
  vt.ready.then(function(){
    document.documentElement.animate(
      { clipPath: ["circle(0px at " + x + "px " + y + "px)",
                   "circle(" + far + "px at " + x + "px " + y + "px)"] },
      { duration: 480, easing: "cubic-bezier(.22,1,.36,1)",
        pseudoElement: "::view-transition-new(root)" }
    );
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

/* ---- segmented controls ---- */
$$("#window button").forEach(function(b){
  b.addEventListener("click", function(){
    S.days = Number(b.dataset.days);
    $$("#window button").forEach(function(o){
      o.setAttribute("aria-checked", String(o === b));
    });
  });
});
$$("#filter button").forEach(function(b){
  b.addEventListener("click", function(){
    S.filter = b.dataset.f;
    $$("#filter button").forEach(function(o){
      o.setAttribute("aria-selected", String(o === b));
    });
    renderFinds();
  });
});

/* ---- counters, as in the metrics block ---- */
function countTo(el, to, suffix){
  var from = Number(el.dataset.v || 0);
  el.dataset.v = to;
  if (matchMedia("(prefers-reduced-motion: reduce)").matches || from === to) {
    el.innerHTML = to + (suffix || ""); return;
  }
  var t0 = performance.now(), ms = 620;
  (function step(now){
    var k = Math.min(1, (now - t0) / ms);
    var e = 1 - Math.pow(1 - k, 3);
    el.innerHTML = Math.round(from + (to - from) * e) + (suffix || "");
    if (k < 1) { requestAnimationFrame(step); }
  })(performance.now());
}

function renderStats(){
  var gs = S.groups;
  var channels = {}, matches = {}, vods = {}, moments = 0;
  gs.forEach(function(g){
    channels[g.channel] = 1; matches[g.match_id] = 1;
    if (!vods[g.vod_id]) { vods[g.vod_id] = g.vod_duration || 0; }
    moments += g.playable || 0;
  });
  var hours = 0;
  Object.keys(vods).forEach(function(v){ hours += vods[v]; });
  hours = Math.round(hours / 360) / 10;

  var tiles = [
    ["Streamers", Object.keys(channels).length, "", "lobbies that were on air"],
    ["Matches",   Object.keys(matches).length,  "", "with a broadcast behind them"],
    ["Moments",   moments,                      "", "inside a playable window"],
    ["Broadcast", hours,                        "h", "of footage reachable"]
  ];
  var box = $("#stats");
  if (!box.children.length) {
    box.innerHTML = tiles.map(function(t, i){
      return '<div class="tile rise" style="animation-delay:' + (i * 0.07) + 's">' +
             '<div class="k">' + esc(t[0]) + '</div>' +
             '<div class="v"><span data-v="0">0</span>' +
             (t[2] ? '<small>' + esc(t[2]) + '</small>' : '') + '</div>' +
             '<div class="s">' + esc(t[3]) + '</div></div>';
    }).join("");
  }
  $$("#stats .v span").forEach(function(el, i){ countTo(el, tiles[i][1], ""); });
}

/* ---- finds list ---- */
function visible(){
  return S.filter === "all" ? S.groups
       : S.groups.filter(function(g){ return g.verdict === "confirmed"; });
}
function renderFinds(){
  var list = $("#findlist"), gs = visible();
  if (!gs.length) {
    list.innerHTML = '<li class="empty" style="padding:40px 16px"><span class="eyebrow">Nothing yet</span>' +
      '<p>' + (S.groups.length ? "No confirmed finds in this account."
                               : "Run a scan to look for lobby-mates who were live.") + '</p></li>';
    return;
  }
  list.innerHTML = gs.map(function(g, i){
    return '<li><button class="find rise" data-id="' + esc(g.id) + '"' +
      ' style="animation-delay:' + Math.min(i * 0.035, 0.4) + 's"' +
      ' aria-current="' + String(S.sel === g.id) + '">' +
      '<span class="r1"><span class="ch">' + esc(g.channel) + '</span>' +
      '<span class="badge ' + esc(g.verdict) + '">' + esc(g.verdict) + '</span>' +
      '<span class="count mono">' + (g.playable || 0) + '</span></span>' +
      '<span class="r2"><span>' + esc(g.map) + '</span><span class="dot"></span>' +
      '<span>' + esc(g.score || g.mode) + '</span><span class="dot"></span>' +
      '<span>' + esc((g.started_text || "").slice(0, 10)) + '</span></span>' +
      '</button></li>';
  }).join("");
  $$("#findlist .find").forEach(function(b){
    b.addEventListener("click", function(){ select(b.dataset.id); });
  });
}

/* ---- the stage: player, scrubber, chapters, moments ---- */
function group(){
  for (var i = 0; i < S.groups.length; i++) {
    if (S.groups[i].id === S.sel) { return S.groups[i]; }
  }
  return null;
}

function renderStage(){
  var g = group(), stage = $("#stage");
  if (!g) {
    stage.innerHTML = '<div class="head"><span class="t">Viewer</span></div>' +
      '<div class="empty"><span class="eyebrow">Nothing selected</span>' +
      '<p>Pick a find on the left to open the broadcast at your own plays.</p></div>';
    return;
  }
  var span = g.span || {start:0, end:1};
  var marks = [];
  (g.rounds || []).forEach(function(r){
    marks.push({cls:"round", at:r.at, label:"Round " + r.round, sub:"starts", off:r.offset, ok:r.playable});
    (r.kills || []).forEach(function(k){
      marks.push({cls:"kill", at:k.at, label:k.victim || "kill",
                  sub:"round " + r.round + (k.weapon ? " / " + k.weapon : ""),
                  off:k.offset, ok:k.playable});
    });
  });
  (g.clips || []).forEach(function(c){
    marks.push({cls:"star", at:c.at, label:c.label, sub:"round " + c.round,
                off:c.offset, ok:c.playable});
  });

  stage.innerHTML =
    '<div class="head">' +
      '<span class="t">' + esc(g.channel) + '</span>' +
      '<span class="t" style="letter-spacing:.18em">' + esc(g.map) + ' &middot; ' +
        esc(g.score || g.mode) + ' &middot; ' + esc(g.timing) + '</span>' +
    '</div>' +
    '<div class="player" id="player"></div>' +
    '<div class="deck">' +
      '<div class="nowline"><span class="n" id="nowname">&mdash;</span>' +
        '<span class="w" id="nowsub"></span>' +
        '<span class="ts mono" id="nowts"></span></div>' +
      '<div class="scrub" id="scrub">' +
        '<div class="rail"></div>' +
        '<div class="playhead" id="playhead"></div>' +
        '<div class="tip" id="tip"></div>' +
      '</div>' +
      '<div class="axis"><span class="mono">' + hhmmss(span.start) + '</span>' +
        '<span class="mono">' + hhmmss(span.end) + '</span></div>' +
      '<div class="chapters" id="chapters"></div>' +
      '<ul class="moments" id="moments"></ul>' +
    '</div>' +
    (g.reasons && g.reasons.length
      ? '<div class="note">' + esc(g.reasons.join("  ·  ")) + '</div>'
      : "");

  /* scrubber marks */
  var scrub = $("#scrub"), tip = $("#tip");
  var at = function(v){
    return Math.max(0, Math.min(100, (v - span.start) / Math.max(1, span.end - span.start) * 100));
  };
  marks.sort(function(a, b){ return a.at - b.at; });
  marks.forEach(function(m){
    var b = document.createElement("button");
    b.type = "button";
    b.className = "mark " + m.cls;
    b.style.left = at(m.at) + "%";
    b.title = m.label;
    b.innerHTML = "<i></i>";
    if (m.ok) { b.addEventListener("click", function(){ seek(m.off, m.label, m.sub); }); }
    b.addEventListener("pointerenter", function(){
      tip.innerHTML = "<b>" + esc(m.label) + "</b><span>" + esc(m.sub) + "</span>";
      tip.style.left = at(m.at) + "%";
      tip.classList.add("on");
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

  /* chapter jumps: ruixenui/audio-timeline-with-chapters */
  $("#chapters").innerHTML = (g.rounds || []).map(function(r){
    return '<button class="chap" type="button" data-off="' + r.offset + '"' +
      (r.playable ? "" : " disabled") + ' data-round="' + r.round + '">' + r.round + '</button>';
  }).join("") || '<span class="lbl">No round clock in this match</span>';
  $$("#chapters .chap").forEach(function(b){
    b.addEventListener("click", function(){
      seek(Number(b.dataset.off), "Round " + b.dataset.round, "start");
    });
  });

  /* the few it thinks are best, listed */
  $("#moments").innerHTML = (g.clips || []).map(function(c){
    return '<li><button class="moment" type="button" data-off="' + c.offset + '"' +
      (c.playable ? "" : " disabled") + '>' +
      '<svg class="star-ico" viewBox="0 0 24 24" fill="currentColor"><path d="m12 2 2.9 6.3 6.8.8-5 4.6 1.3 6.7L12 17l-6 3.4 1.3-6.7-5-4.6 6.8-.8z"/></svg>' +
      '<span><span class="lab">' + esc(c.label) + '</span> ' +
      '<span class="sub">round ' + c.round + '</span></span>' +
      '<span class="st mono' + (c.playable ? "" : " no") + '">' +
        esc(c.playable ? c.stamp : "outside") + '</span></button></li>';
  }).join("") || '<li class="lbl" style="padding:6px 8px">No standout moments in this one</li>';
  $$("#moments .moment").forEach(function(b){
    b.addEventListener("click", function(){
      var lab = $(".lab", b);
      seek(Number(b.dataset.off), lab ? lab.textContent : "Moment", "");
    });
  });

  mountPlayer(g);
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

  function iframe(){
    host.innerHTML = '<iframe allowfullscreen src="https://player.twitch.tv/?video=v' +
      encodeURIComponent(g.vod_id) + "&time=" + hhmmss(start).replace(/:/g, "") +
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
        /* which round the playhead is actually inside */
        var cur = null;
        (g.rounds || []).forEach(function(r){ if (r.at <= t) { cur = r.round; } });
        $$("#chapters .chap").forEach(function(b){
          b.setAttribute("aria-current", String(Number(b.dataset.round) === cur));
        });
      }, 500);
    } catch (e) { iframe(); }
  }
  if (window.Twitch && window.Twitch.Player) { build(); return; }
  var s = document.createElement("script");
  s.src = "https://embed.twitch.tv/embed/v1.js";
  s.onload = build;
  s.onerror = iframe;
  document.head.appendChild(s);
  setTimeout(function(){ if (!S.player && !host.children.length) { iframe(); } }, 4000);
}

function seek(sec, label, sub){
  var n = $("#nowname"), s = $("#nowsub");
  if (n) { n.textContent = label || ""; }
  if (s) { s.textContent = sub || ""; }
  if (S.player && S.player.seek) { try { S.player.seek(sec); S.player.play && S.player.play(); return; } catch (e) {} }
  var g = group(); if (!g) { return; }
  var host = $("#player");
  if (host) {
    host.innerHTML = '<iframe allowfullscreen src="https://player.twitch.tv/?video=v' +
      encodeURIComponent(g.vod_id) + "&time=" + hhmmss(sec).replace(/:/g, "") +
      "&parent=" + encodeURIComponent(location.hostname) + '"></iframe>';
  }
}

function select(id){
  S.sel = id;
  $$("#findlist .find").forEach(function(b){
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
    renderStats(); renderFinds();
    if (!group()) { S.sel = S.groups.length ? S.groups[0].id : null; }
    renderStage();
    if (S.sel) { select(S.sel); }
    $("#footright").textContent = (j.summary && j.summary.matches || 0) + " matches stored";
  });
}

function boot(){
  api("/api/state").then(function(j){
    $("#riot").value = j.default_riot_id || "";
    $("#who").innerHTML = j.default_riot_id
      ? esc(j.default_riot_id.split("#")[0]) + '<span class="tag">#' +
        esc(j.default_riot_id.split("#")[1] || "") + "</span>"
      : "Find your plays";
    if (j.region) { var c = $("#region"); c.hidden = false; c.textContent = j.region; }
    S.hasTwitch = !!j.has_twitch;
    if (!j.has_twitch) { fail("No Twitch credentials configured, so nothing can be confirmed from a broadcast."); }
    if (j.job && j.job.state === "running") { watch(); }
    return loadClips(j.default_key || "");
  }).catch(function(e){ fail(String(e.message || e)); });
}

/* ---- scan + progress ---- */
var PHASES = {starting:"Starting", ingest:"Reading match history", resolve:"Checking the lobby", done:"Done"};
function watch(){
  $("#prog").hidden = false;
  $("#go").disabled = true;
  if (S.poll) { clearInterval(S.poll); }
  S.poll = setInterval(poll, 700);
  poll();
}
function poll(){
  api("/api/job").then(function(j){
    if (!j || j.state === "idle") { return; }
    $("#pphase").textContent = PHASES[j.phase] || j.phase || "Working";
    var fill = $("#pfill");
    if (j.total > 0) {
      fill.classList.remove("indet");
      var pct = Math.round(j.done / j.total * 100);
      fill.style.width = pct + "%";
      $("#ppct").textContent = j.done + " / " + j.total + "  " + j.elapsed + "s";
    } else {
      fill.classList.add("indet"); fill.style.width = "";
      $("#ppct").textContent = j.elapsed + "s";
    }
    $("#log").textContent = (j.lines || []).join("\n");
    var log = $("#log"); log.scrollTop = log.scrollHeight;
    if (j.state !== "running") {
      clearInterval(S.poll); S.poll = null;
      $("#go").disabled = false;
      fill.classList.remove("indet"); fill.style.width = "100%";
      $("#pphase").textContent = j.state === "error" ? "Failed" : "Done";
      if (j.error) { fail(j.error); }
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
    body: JSON.stringify({riot_id: riot, days: S.days, watch: true})
  }).then(function(){ $("#log").textContent = ""; watch(); })
    .catch(function(e){ fail(String(e.message || e)); });
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
  if (e.key === "/" ) { e.preventDefault(); $("#riot").focus(); $("#riot").select(); }
  if (e.key === "j" || e.key === "k") {
    var gs = visible(); if (!gs.length) { return; }
    var i = 0;
    for (var n = 0; n < gs.length; n++) { if (gs[n].id === S.sel) { i = n; } }
    i = Math.max(0, Math.min(gs.length - 1, i + (e.key === "j" ? 1 : -1)));
    select(gs[i].id);
    var el = $('#findlist .find[data-id="' + gs[i].id + '"]');
    if (el) { el.scrollIntoView({block: "nearest"}); }
  }
});

boot();
</script>
</body>
</html>
"""
