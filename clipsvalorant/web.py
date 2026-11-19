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

# Both ends of a clip are clamped into the broadcast. An offset past the end of
# the video is worse than no link: Twitch answers it with the last frame and no
# error, so it looks like the detector was wrong rather than like the stream
# having ended early.
MAX_JOB_LINES = 400
MAX_SEARCH_LIMIT = 20

VERDICT_ORDER = {"confirmed": 0, "likely": 1, "unconfirmed": 2, "rejected": 3}


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


_CLIP_SQL = """
SELECT e.match_id, e.riot_id, e.channel_login, e.vod_id, e.verdict,
       e.same_team, e.evidence, e.verified_how,
       m.started_at_epoch, m.map, m.mode, m.queue,
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


def clip_groups(conn: sqlite3.Connection, *, lead: int = LEAD_S) -> list[dict[str, Any]]:
    """One group per (match, streamer), each holding its playable moments.

    A highlight is only playable if the broadcast actually contains it. Two
    things can put it outside: a stream that began after the moment, and one
    that ended before it. Both are reported rather than dropped - a streamer
    who joined at round 14 is a real find, and silently showing nothing for
    them looks identical to a bug.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute(_CLIP_SQL):
        key = (row["match_id"], row["channel_login"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
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
                "vod_title": row["title"] or "",
                "reasons": _evidence_reasons(row["evidence"]),
                "timing": row["method"] or "estimated",
                "shift_s": row["shift_s"] if row["shift_s"] is not None
                           else calibrate.FALLBACK_SHIFT_S,
                "clips": [],
            }
        offset = calibrate.moment_offset(
            match_start_epoch=row["started_at_epoch"],
            vod_created_epoch=row["created_epoch"],
            time_in_match_ms=row["time_in_match_ms"],
            shift_s=group["shift_s"],
            lead_s=lead,
        )
        duration = row["duration_s"] or 0
        if offset < 0:
            why = f"the stream began {abs(offset)}s after this moment"
        elif duration and offset > duration:
            why = f"the stream ended {offset - duration}s before this moment"
        else:
            why = ""
        group["clips"].append({
            "round": row["round"],
            "label": row["label"],
            "kind": row["kind"],
            "kills": row["kill_count"],
            "won": None if row["round_won"] is None else bool(row["round_won"]),
            "offset": max(offset, 0),
            "stamp": calibrate.stamp(offset),
            "playable": not why,
            "why_not": why,
            "url": (
                f"https://www.twitch.tv/videos/{row['vod_id']}"
                f"?t={calibrate.stamp(offset)}"
            ),
        })

    out = list(groups.values())
    for group in out:
        group["playable"] = sum(1 for c in group["clips"] if c["playable"])
    out.sort(
        key=lambda g: (VERDICT_ORDER.get(g["verdict"], 9), -g["started"])
    )
    return out


def account_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM match) AS matches,"
        " (SELECT COUNT(*) FROM highlight) AS highlights,"
        " (SELECT COUNT(*) FROM encounter) AS encounters,"
        " (SELECT COUNT(*) FROM encounter WHERE verdict = 'rejected') AS rejected"
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

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            lines = list(self.lines)
        return {
            "id": self.id,
            "riot_id": self.riot_id,
            "key": self.key,
            "state": self.state,
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
    limit: int = 5,
    mode: str | None = "competitive",
    watch: bool = True,
    region: str | None = None,
) -> None:
    """Ingest then resolve one account, narrating as it goes."""
    from clipsvalorant.ingest import ingest
    from clipsvalorant.resolve import resolve_recent

    acct = config_for(cfg, job.riot_id, region=region)
    job.key = acct.db_path.stem
    handler = _JobLog(job)
    package = logging.getLogger("clipsvalorant")
    package.addHandler(handler)
    previous = package.level
    package.setLevel(logging.INFO)
    try:
        job.say(f"Looking up {acct.riot_id} in {acct.region.upper()}...")
        report = ingest(acct, limit=limit, mode=mode)
        job.say(report.summary())
        for err in report.errors[:5]:
            job.say(f"! {err}")

        with db.session(acct.db_path) as conn:
            ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM match ORDER BY started_at_epoch DESC LIMIT ?",
                    (limit,),
                )
            ]
        if not ids:
            job.say(
                "No matches stored for this account. HenrikDev only keeps matches "
                "somebody has already asked it for, so a brand new name can come "
                "back empty - try another queue, or play a game and search again."
            )
            job.state = "done"
            return

        if not acct.has_twitch:
            job.say(
                "Twitch credentials are not configured, so the streamer search "
                "cannot run. Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env."
            )
            job.state = "done"
            return

        job.say(
            f"Checking {len(ids)} match(es) for players who were streaming"
            + (" (reading the broadcasts, which is the slow part)" if watch else "")
            + "..."
        )
        done = 0

        def tick(match_id: str, report: Any) -> None:
            nonlocal done
            done += 1
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
                limit = max(1, min(MAX_SEARCH_LIMIT, int(body.get("limit") or 5)))
            except (TypeError, ValueError):
                limit = 5
            mode = str(body.get("mode") or "").strip().lower() or None
            if mode == "any":
                mode = None
            region = str(body.get("region") or "").strip().lower() or None
            try:
                job = state.start(
                    riot_id,
                    limit=limit,
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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>clipsvalorant</title>
<style>
  :root {
    --bg: #0f1115; --panel: #171a21; --line: #272b36; --text: #e6e8ee;
    --dim: #8b93a7; --ok: #4ade80; --warn: #fbbf24; --accent: #ff4655;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 ui-sans-serif, system-ui, "Segoe UI", sans-serif;
  }
  header {
    position: sticky; top: 0; z-index: 10; background: var(--panel);
    border-bottom: 1px solid var(--line); padding: 10px 16px;
  }
  h1 { font-size: 15px; margin: 0 0 8px; letter-spacing: .04em; }
  h1 span { color: var(--accent); }
  form { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  input, select, button {
    background: #0b0d12; color: var(--text); border: 1px solid var(--line);
    border-radius: 6px; padding: 7px 9px; font: inherit;
  }
  input[type=text] { min-width: 210px; }
  button { cursor: pointer; background: #222736; }
  button:hover:not(:disabled) { border-color: var(--dim); }
  button:disabled { opacity: .5; cursor: default; }
  button.go { background: var(--accent); border-color: var(--accent); color: #fff; }
  label.check { color: var(--dim); display: flex; align-items: center; gap: 5px; }
  .wrap { display: grid; grid-template-columns: minmax(0,1fr) 420px; gap: 16px; padding: 16px; }
  @media (max-width: 950px) { .wrap { grid-template-columns: minmax(0,1fr); } }
  .player { position: sticky; top: 92px; }
  .frame {
    aspect-ratio: 16/9; width: 100%; background: #000;
    border: 1px solid var(--line); border-radius: 8px; overflow: hidden;
  }
  .frame iframe { width: 100%; height: 100%; border: 0; display: block; }
  .frame .empty {
    display: grid; place-items: center; height: 100%; color: var(--dim);
    text-align: center; padding: 20px; font-size: 13px;
  }
  .now { color: var(--dim); font-size: 12px; margin-top: 8px; min-height: 34px; }
  .now b { color: var(--text); }
  /* The browser default is near-black on this background. */
  a { color: #7aa2f7; }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 8px; padding: 12px 14px; margin-bottom: 12px;
  }
  .card.confirmed { border-left: 3px solid var(--ok); }
  .card.likely, .card.unconfirmed { border-left: 3px solid var(--warn); }
  .who { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
  .who a { color: var(--text); font-weight: 600; text-decoration: none; }
  .who a:hover { color: var(--accent); }
  .tag {
    font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
    padding: 1px 6px; border-radius: 10px; border: 1px solid var(--line); color: var(--dim);
  }
  .tag.confirmed { color: var(--ok); border-color: #255c3a; }
  .tag.likely, .tag.unconfirmed { color: var(--warn); border-color: #5c4a19; }
  .meta { color: var(--dim); font-size: 12px; margin: 4px 0 10px; }
  .clips { display: flex; flex-wrap: wrap; gap: 6px; }
  .clip { text-align: left; padding: 6px 10px; line-height: 1.3; }
  .clip small { display: block; color: var(--dim); font-size: 11px; }
  .clip.playing { border-color: var(--accent); background: #2a1c21; }
  .clip.dead { opacity: .45; cursor: not-allowed; }
  details { margin-top: 10px; }
  summary { cursor: pointer; color: var(--dim); font-size: 12px; }
  details ul { margin: 6px 0 0; padding-left: 18px; color: var(--dim); font-size: 12px; }
  .status { padding: 0 16px; }
  .status .bar {
    background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
    padding: 10px 12px; font-size: 13px;
  }
  .log {
    margin: 8px 0 0; max-height: 190px; overflow: auto; white-space: pre-wrap;
    font: 12px/1.45 ui-monospace, Consolas, monospace; color: var(--dim);
  }
  .note { color: var(--dim); font-size: 13px; }
  .err { color: var(--accent); }
  .warn { color: var(--warn); }
  .spin {
    display: inline-block; width: 11px; height: 11px; margin-right: 7px;
    border: 2px solid var(--line); border-top-color: var(--accent);
    border-radius: 50%; animation: spin .8s linear infinite; vertical-align: -1px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<header>
  <h1>clips<span>valorant</span></h1>
  <form id="search">
    <input type="text" id="riot" placeholder="PlayerOne#NA1" autocomplete="off" spellcheck="false">
    <select id="region">
      <option value="na">NA</option><option value="eu">EU</option>
      <option value="ap">AP</option><option value="kr">KR</option>
      <option value="latam">LATAM</option><option value="br">BR</option>
    </select>
    <select id="mode">
      <option value="competitive">Competitive</option>
      <option value="unrated">Unrated</option>
      <option value="any">Any mode</option>
    </select>
    <select id="limit">
      <option value="3">3 matches</option>
      <option value="5" selected>5 matches</option>
      <option value="10">10 matches</option>
      <option value="20">20 matches</option>
    </select>
    <label class="check"><input type="checkbox" id="watch" checked> read the VODs</label>
    <button class="go" id="go" type="submit">Search</button>
    <select id="saved" title="Accounts already searched on this machine"></select>
  </form>
</header>

<div class="status" id="status" hidden><div class="bar">
  <div id="statusLine"></div>
  <div class="log" id="log"></div>
</div></div>

<div class="wrap">
  <main id="results"><p class="note">Loading...</p></main>
  <aside>
    <div class="player">
      <div class="frame" id="frame">
        <div class="empty">Pick a moment on the left and it plays here,
          from the streamer's point of view.</div>
      </div>
      <div class="now" id="now"></div>
    </div>
  </aside>
</div>

<script>
const $ = (id) => document.getElementById(id);
const PARENT = location.hostname || "localhost";
let polling = null;
let current = null;

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || (res.status + " " + res.statusText));
  return data;
}

function play(group, clip, button) {
  // The player is rebuilt rather than re-seeked. Twitch's embed exposes no way
  // to move an existing player from the host page, so a new src is the seek.
  const src = "https://player.twitch.tv/?video=v" + encodeURIComponent(group.vod_id)
    + "&time=" + encodeURIComponent(clip.stamp)
    + "&parent=" + encodeURIComponent(PARENT)
    + "&autoplay=true&muted=false";
  $("frame").innerHTML = '<iframe src="' + src
    + '" allow="autoplay; fullscreen"></iframe>';
  $("now").innerHTML = "<b>" + esc(clip.label) + "</b>, round " + clip.round
    + " &middot; " + esc(group.map) + " &middot; from twitch.tv/"
    + esc(group.channel) + " at " + esc(clip.stamp)
    + '<br><a href="' + esc(clip.url) + '" target="_blank" rel="noreferrer">'
    + "open on Twitch instead</a> if the embed refuses to play";
  document.querySelectorAll(".clip.playing").forEach((b) => b.classList.remove("playing"));
  if (button) button.classList.add("playing");
  current = clip;
}

function card(group) {
  const el = document.createElement("div");
  el.className = "card " + group.verdict;
  const side = group.same_team ? "your team" : "enemy team";
  el.innerHTML =
    '<div class="who">'
    + '<a href="https://twitch.tv/' + esc(group.channel) + '" target="_blank" '
    + 'rel="noreferrer">twitch.tv/' + esc(group.channel) + "</a>"
    + '<span class="tag ' + group.verdict + '">' + esc(group.verdict) + "</span>"
    + '<span class="tag">' + esc(side) + "</span></div>"
    + '<div class="meta">' + esc(group.riot_id) + " &middot; " + esc(group.map)
    + " &middot; " + esc(group.mode) + " &middot; " + esc(group.started_text)
    + (group.vod_title ? " &middot; &ldquo;" + esc(group.vod_title) + "&rdquo;" : "")
    // A match's own timestamp is the queue popping, not the map loading, so
    // the gap was measured off the screen. Where it could not be, the median
    // stands in and the timings are a few seconds loose - say which.
    + (group.timing === "measured" ? ""
        : '<br><span class="warn">start of match not read off this broadcast'
          + " &mdash; these timings use the "
          + Math.round(group.shift_s) + "s median and may be seconds out</span>")
    + "</div>";

  const row = document.createElement("div");
  row.className = "clips";
  group.clips.forEach((clip) => {
    const b = document.createElement("button");
    b.className = "clip" + (clip.playable ? "" : " dead");
    b.innerHTML = esc(clip.label) + " <small>round " + clip.round + " &middot; "
      + (clip.playable ? esc(clip.stamp) : esc(clip.why_not)) + "</small>";
    if (clip.playable) b.onclick = () => play(group, clip, b);
    else b.disabled = true;
    row.appendChild(b);
  });
  el.appendChild(row);

  if (group.reasons && group.reasons.length) {
    const d = document.createElement("details");
    d.innerHTML = "<summary>why we think this is them</summary><ul>"
      + group.reasons.map((r) => "<li>" + esc(r) + "</li>").join("") + "</ul>";
    el.appendChild(d);
  }
  return el;
}

function render(data) {
  const out = $("results");
  out.innerHTML = "";
  const groups = data.groups || [];
  if (!groups.length) {
    const s = data.summary || {};
    out.innerHTML = '<p class="note">'
      + (data.error ? esc(data.error) + ". " : "")
      + "No streamers to show for this account."
      + (s.matches ? " " + s.matches + " match(es) and " + (s.highlights || 0)
          + " highlight(s) are stored; " + (s.rejected || 0)
          + " broadcast(s) were checked and ruled out." : "")
      + " Twitch keeps past broadcasts for 7-14 days, so matches older than that"
      + " usually have nothing left to link to.</p>";
    return;
  }
  const confirmed = groups.filter((g) => g.verdict === "confirmed");
  const rest = groups.filter((g) => g.verdict !== "confirmed");
  if (confirmed.length) {
    out.insertAdjacentHTML("beforeend",
      "<p class=note><b>" + confirmed.length + " confirmed</b> &mdash; the lobby was"
      + " read off the screen, or a clip was cut from inside the match.</p>");
    confirmed.forEach((g) => out.appendChild(card(g)));
  }
  if (rest.length) {
    out.insertAdjacentHTML("beforeend",
      "<p class=note style='margin-top:18px'><b>" + rest.length + " unverified</b>"
      + " &mdash; these channels were broadcasting across the match, but nothing"
      + " proves they were in it. Click one before believing it.</p>");
    rest.forEach((g) => out.appendChild(card(g)));
  }
}

async function loadClips(key) {
  try {
    render(await api("/api/clips?key=" + encodeURIComponent(key || "")));
  } catch (e) {
    $("results").innerHTML = '<p class="err">' + esc(e.message) + "</p>";
  }
}

function showJob(job) {
  const box = $("status");
  if (!job || job.state === "idle") { box.hidden = true; return; }
  box.hidden = false;
  const running = job.state === "running";
  $("go").disabled = running;
  const head = running
    ? '<span class="spin"></span>Searching ' + esc(job.riot_id)
      + " &middot; " + job.elapsed + "s"
    : (job.state === "error"
        ? '<span class="err">Search for ' + esc(job.riot_id) + " failed</span>"
        : "Finished " + esc(job.riot_id) + " in " + job.elapsed + "s");
  $("statusLine").innerHTML = head;
  $("log").textContent = (job.lines || []).join("\n");
  $("log").scrollTop = $("log").scrollHeight;
  if (!running && polling) {
    clearInterval(polling);
    polling = null;
    // Select the account the search just wrote to. Without this the list keeps
    // whatever was chosen before, and the clips below it come from a different
    // database than the one the dropdown names.
    refresh(job.key).then(() => loadClips(job.key || $("saved").value));
  }
}

function watchJob() {
  if (polling) clearInterval(polling);
  polling = setInterval(async () => {
    try { showJob(await api("/api/job")); } catch (e) { /* keep polling */ }
  }, 1200);
}

async function refresh(prefer) {
  const state = await api("/api/state");
  const sel = $("saved");
  const keep = prefer || sel.value;
  const accounts = state.accounts || [];
  // One Riot ID can own two databases - a file from an earlier run beside the
  // one this UI writes - and two options reading alike is worse than a long
  // label, so the filename breaks the tie.
  const seen = {};
  accounts.forEach((a) => { seen[a.riot_id] = (seen[a.riot_id] || 0) + 1; });
  sel.innerHTML = "";
  accounts.forEach((a) => {
    const o = document.createElement("option");
    o.value = a.key;
    o.textContent = a.riot_id + "  (" + a.matches + " matches, "
      + a.confirmed + " confirmed)" + (seen[a.riot_id] > 1 ? "  [" + a.key + "]" : "");
    sel.appendChild(o);
  });
  if (!sel.options.length) {
    sel.innerHTML = "<option value=''>nothing searched yet</option>";
  }
  sel.value = keep && [...sel.options].some((o) => o.value === keep)
    ? keep : (state.default_key || sel.options[0].value);
  if (!$("riot").value) $("riot").placeholder = state.default_riot_id;
  $("region").value = state.region || "na";
  if (!state.has_twitch) {
    $("statusLine").innerHTML = '<span class="err">Twitch credentials are not'
      + " configured, so searching stops after ingest.</span>";
    $("status").hidden = false;
  }
  return state;
}

$("saved").onchange = () => loadClips($("saved").value);

$("search").onsubmit = async (ev) => {
  ev.preventDefault();
  const riot = $("riot").value.trim() || $("riot").placeholder;
  try {
    showJob(await api("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        riot_id: riot,
        region: $("region").value,
        mode: $("mode").value,
        limit: Number($("limit").value),
        watch: $("watch").checked,
      }),
    }));
    watchJob();
  } catch (e) {
    $("status").hidden = false;
    $("statusLine").innerHTML = '<span class="err">' + esc(e.message) + "</span>";
  }
};

(async () => {
  const state = await refresh();
  await loadClips($("saved").value);
  if (state.job) { showJob(state.job); if (state.job.state === "running") watchJob(); }
})();
</script>
</body>
</html>
"""
