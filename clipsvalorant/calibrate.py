"""Where a match actually sits inside a broadcast.

The match data carries one wall-clock timestamp, `metadata.started_at`, and
one in-match clock, `time_in_match_in_ms`. They do not share a zero, and the
gap between them is the whole reason this module exists.

`started_at` is when the game server session began - the moment the queue
pops and agent select opens. `time_in_match_in_ms` counts from when the map
loads, after agent select and after every player's client has finished
loading. Measured against three broadcasts the gap was 101, 109 and 125
seconds: about a round and a half, and variable, because loading takes as
long as the slowest machine in the lobby takes.

Adding the two together, which is the obvious thing to do and what this
project did at first, puts every link roughly one round early. It is a
silent failure. The link lands inside the right match on the right map, the
detection it came from is correct, and the moment is simply not there.

So the offset is measured rather than assumed. Valorant draws a countdown at
the top centre of the screen in every round - the buy phase counts down to
the barriers dropping, the round itself counts down from 1:40 - and the kill
data says exactly when each round began on the in-match clock. Reading that
countdown off a frame therefore dates the frame precisely, and the
difference between where the frame is in the broadcast and where it is in
the match is the number we want.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Sequence

from clipsvalorant import db
from clipsvalorant.vision import TIMER_RE, FrameRead, Reader
from clipsvalorant.vod import FrameReader

log = logging.getLogger(__name__)

# Valorant's round timer. The buy phase has its own, shorter countdown, but
# that one is read against the round start rather than assumed.
ROUND_TIMER_S = 100.0

# A match cannot plausibly begin less than a minute or more than four minutes
# after the server session opens: agent select alone is around ninety seconds.
# The bound is what keeps a misread timer from being taken seriously.
SHIFT_MIN_S = 45.0
SHIFT_MAX_S = 260.0

# Two readings count as the same answer within this many seconds. Frames are
# whole seconds and the countdown ticks once a second, so this is tight.
CLUSTER_S = 2.5

# Three frames agreeing is not a coincidence: each one is a different round
# position, and round lengths are irregular, so a wrong shift cannot satisfy
# several of them at once.
MIN_VOTES = 3

SCAN_STEP_S = 15
SCAN_FRAMES = 13

# Used when a broadcast cannot be read - no OCR installed, or the stream
# joined too late to show a countdown. The median of the measurements above.
# It is better than nothing and worse than measuring, and the two are kept
# distinguishable everywhere so a link is never presented as more precise
# than it is.
FALLBACK_SHIFT_S = 109.0

_BUY_WORDS = ("buyphase", "pressbtobuy")


@dataclass(frozen=True)
class Calibration:
    """Seconds to add to a naive offset, and whether anybody checked."""

    shift_s: float
    method: str  # "measured" | "estimated"
    votes: int = 0
    vod_id: str = ""

    @property
    def measured(self) -> bool:
        return self.method == "measured"


ESTIMATE = Calibration(FALLBACK_SHIFT_S, "estimated")


def _field(kill: object, name: str) -> object | None:
    if isinstance(kill, dict):
        return kill.get(name)
    return getattr(kill, name, None)


def round_starts(kills: Iterable[object]) -> dict[int, float]:
    """When each round's barriers dropped, on the in-match clock.

    Riot gives both clocks on every kill, so subtracting one from the other
    dates the round itself. The first kill of a round is enough; later ones
    agree to the millisecond.

    Takes parsed Kill models or the raw dicts, because the callers that have
    one rarely have the other: resolving works from the model, and anything
    re-deriving from `match.raw_json` works from the dict.
    """
    starts: dict[int, float] = {}
    for kill in kills:
        rnd = _field(kill, "round")
        in_match = _field(kill, "time_in_match_in_ms")
        in_round = _field(kill, "time_in_round_in_ms")
        if rnd is None or in_match is None or in_round is None:
            continue
        starts.setdefault(int(rnd), (in_match - in_round) / 1000.0)
    return starts


def read_timer(frame: FrameRead) -> float | None:
    """The countdown at the top centre of the screen, in seconds.

    Restricted to the middle of the top strip because it is not the only
    MM:SS on screen - a stream overlay or a Spotify widget can carry one too,
    and one of those read as a round timer would date the frame confidently
    and wrongly.
    """
    if not frame.width or not frame.height:
        return None
    best: tuple[float, float] | None = None
    for box in frame.boxes:
        text = (box.text or "").strip()
        if not TIMER_RE.match(text):
            continue
        # Boxes come back in pixels; the bands are fractions of the frame.
        mid_x = (box.x0 + box.x1) / 2 / frame.width
        top = box.y1 / frame.height
        if not 0.40 <= mid_x <= 0.60 or top > 0.12:
            continue
        minutes, _, seconds = text.partition(":")
        try:
            value = int(minutes) * 60 + int(seconds)
        except ValueError:
            continue
        if value > 180:  # no Valorant countdown runs this long
            continue
        if best is None or top < best[1]:
            best = (float(value), top)
    return None if best is None else best[0]


def votes_from(
    offset_in_vod: float, base: float, timer_s: float, is_buy: bool,
    starts: dict[int, float],
) -> list[float]:
    """Every shift that would explain this frame showing this countdown.

    One frame cannot say which round it is looking at, so it offers one
    answer per round and the answer that keeps turning up across frames wins.
    Both phases are offered unless the screen said "BUY PHASE" outright,
    because a bare countdown of 0:20 is equally a buy phase about to end and
    a round eighty seconds old.
    """
    out: list[float] = []
    for start in starts.values():
        moments = [start - timer_s]  # counting down to the barriers dropping
        if not is_buy:
            moments.append(start + (ROUND_TIMER_S - timer_s))  # into the round
        for moment in moments:
            shift = offset_in_vod - base - moment
            if SHIFT_MIN_S <= shift <= SHIFT_MAX_S:
                out.append(shift)
    return out


def best_cluster(votes: Sequence[float]) -> tuple[float, int]:
    """The tightest agreement among the candidate shifts, and how big it was."""
    if not votes:
        return 0.0, 0
    ordered = sorted(votes)
    best_centre, best_n = 0.0, 0
    for anchor in ordered:
        near = [v for v in ordered if abs(v - anchor) <= CLUSTER_S]
        if len(near) > best_n:
            best_n = len(near)
            best_centre = sum(near) / len(near)
    return round(best_centre, 1), best_n


def scan_offsets(
    base: float, window: tuple[float, float], *,
    limit: int = SCAN_FRAMES, step: int = SCAN_STEP_S,
) -> list[float]:
    """Where to look, in broadcast time.

    Starts a minute after the naive match start, which is before any shift in
    the plausible range has the map loaded, and walks forward through the
    first rounds. `window` is the stretch the broadcast actually covers, so a
    stream that joined late is scanned from where it joined.
    """
    lo, hi = window
    start = max(lo, base + 60)
    out = [start + i * step for i in range(limit)]
    return [o for o in out if lo <= o <= hi]


def _votes_in(reads: Iterable[tuple[float, object]], base: float,
              starts: dict[int, float]) -> list[float]:
    """Every shift the given frames vote for, pooled."""
    votes: list[float] = []
    for offset, frame in reads:
        if frame is None:
            continue
        timer = read_timer(frame)  # type: ignore[arg-type]
        if timer is None:
            continue
        blob = frame.blob  # type: ignore[attr-defined]
        votes.extend(votes_from(
            offset, base, timer,
            any(word in blob for word in _BUY_WORDS), starts,
        ))
    return votes


def from_reads(
    reads: Iterable[tuple[float, object]], *,
    base: float, starts: dict[int, float], vod_id: str = "",
) -> Calibration:
    """A shift derived from frames somebody else already paid to read.

    Looking for lobby names and looking for the round countdown are the same
    act with two questions asked of it, and they used to be two passes over
    the same broadcast - ten frames to confirm the streamer, then thirteen
    more to time the match. The frames the first pass read carry a countdown
    as often as any others, so the second pass is usually unnecessary.

    It is not always enough: a pass that stopped after three frames, or one
    that landed on scoreboards with the clock hidden, will not reach
    MIN_VOTES. `measure` is still there for that, and still costs what it
    always did - it is just no longer the common case.
    """
    if not starts:
        return ESTIMATE
    shift, agreed = best_cluster(_votes_in(reads, base, starts))
    if agreed < MIN_VOTES:
        return ESTIMATE
    return Calibration(shift, "measured", agreed, vod_id)


def measure(
    reader: Reader, frames: FrameReader, vod_id: str, *,
    base: float, starts: dict[int, float], window: tuple[float, float],
    limit: int = SCAN_FRAMES, workers: int = 4,
) -> Calibration:
    """Read the opening of a broadcast and work out where the match begins."""
    if not starts or not reader.available:
        return ESTIMATE
    offsets = scan_offsets(base, window, limit=limit)
    if not offsets:
        return ESTIMATE

    reads: list[tuple[float, object]] = []
    for offset, path in frames.iter_frames(vod_id, offsets, ahead=workers):
        if path is None:
            continue
        frame = reader.read(path)
        if frame is not None:
            reads.append((offset, frame))

    shift, agreed = best_cluster(_votes_in(reads, base, starts))
    if agreed < MIN_VOTES:
        log.debug("calibration of %s inconclusive (%d agreeing)", vod_id, agreed)
        return ESTIMATE
    return Calibration(shift, "measured", agreed, vod_id)


# ---------------------------------------------------------------- storage

def load(conn: sqlite3.Connection, match_id: str, vod_id: str) -> Calibration:
    row = conn.execute(
        "SELECT shift_s, method, votes FROM match_vod_offset "
        "WHERE match_id = ? AND vod_id = ?",
        (match_id, vod_id),
    ).fetchone()
    if row is None:
        return ESTIMATE
    return Calibration(row["shift_s"], row["method"], row["votes"], vod_id)


def store(
    conn: sqlite3.Connection, match_id: str, vod_id: str, cal: Calibration
) -> None:
    """Kept per broadcast, not per match.

    The gap itself belongs to the match - it is the server's own clock either
    side of agent select, identical for all ten players. The measurement does
    not: a stream that dropped and came back has a broadcast shorter than the
    wall-clock it spans, and everything after the gap sits earlier in the
    video than it should. A number measured from one broadcast would be wrong
    for the next one.
    """
    conn.execute(
        "INSERT INTO match_vod_offset (match_id, vod_id, shift_s, method, votes, "
        "measured_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(match_id, vod_id) DO UPDATE SET "
        "shift_s = excluded.shift_s, method = excluded.method, "
        "votes = excluded.votes, measured_at = excluded.measured_at",
        (match_id, vod_id, cal.shift_s, cal.method, cal.votes, db.utcnow()),
    )


def moment_offset(
    *, match_start_epoch: int, vod_created_epoch: int, time_in_match_ms: int,
    shift_s: float = FALLBACK_SHIFT_S, lead_s: int = 0,
) -> int:
    """Where a moment of the match sits in a broadcast, in seconds.

    The one place this arithmetic is written down. It was duplicated in the
    CLI and the web UI, which is how both of them came to be wrong in exactly
    the same way.
    """
    return int(round(
        (match_start_epoch - vod_created_epoch)
        + shift_s
        + time_in_match_ms / 1000.0
        - lead_s
    ))


def stamp(offset_s: int) -> str:
    offset_s = max(offset_s, 0)
    hours, rest = divmod(offset_s, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}h{minutes:02d}m{seconds:02d}s"


def backfill(cfg, *, limit: int = 50, force: bool = False) -> list[str]:
    """Measure every surviving encounter that has not been measured yet.

    Every link produced before this module existed is about a round early,
    and the matches they point at are still in the database with their
    broadcasts still live. Re-resolving them would cost hundreds of Twitch
    calls and re-decide questions already settled; this only reads frames.
    """
    import json

    from clipsvalorant.models import Match
    from clipsvalorant.resolve import build_watcher

    notes: list[str] = []
    with db.session(cfg.db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT e.match_id, e.vod_id, e.channel_login,
                   m.started_at_epoch, m.raw_json, m.map,
                   v.created_epoch, v.end_epoch
            FROM encounter e
            JOIN match m      ON m.id = e.match_id
            JOIN twitch_vod v ON v.id = e.vod_id
            WHERE e.vod_id IS NOT NULL AND e.verdict IN ('confirmed', 'likely')
            ORDER BY m.started_at_epoch DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        if not rows:
            return ["Nothing confirmed to calibrate."]

        watcher, why = build_watcher(cfg.db_path.parent / "frames")
        if watcher is None:
            return [f"Cannot read broadcasts: {why}"]

        for row in rows:
            if not force and load(conn, row["match_id"], row["vod_id"]).measured:
                continue
            start = row["started_at_epoch"]
            base = start - row["created_epoch"]
            match = Match.model_validate(json.loads(row["raw_json"]))
            length = (match.metadata.game_length_in_ms or 0) / 1000.0
            lo = max(base, 0.0)
            hi = min(base + length, row["end_epoch"] - row["created_epoch"])
            cal = measure(
                watcher.reader, watcher.frames, row["vod_id"],
                base=base, starts=round_starts(match.kills), window=(lo, hi),
            )
            where = f"{row['map']} x twitch.tv/{row['channel_login']}"
            if cal.measured:
                store(conn, row["match_id"], row["vod_id"], cal)
                conn.commit()
                notes.append(
                    f"  {where}: starts {cal.shift_s:.0f}s later than its own "
                    f"timestamp says ({cal.votes} frames agreed)"
                )
            else:
                notes.append(f"  {where}: could not read a countdown; using the median")
    return notes
