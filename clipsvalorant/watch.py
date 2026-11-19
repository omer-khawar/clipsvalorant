"""Choosing which seconds of a broadcast to look at.

Opening a VOD is cheap but not free -- roughly three seconds per frame, between
the seek and the OCR -- so the order in which we sample matters more than the
number of frames we are willing to spend.

Two kinds of moment put names on screen, and we know when both happen because
the match history gives us every kill to the millisecond:

  * the end of a round, when the scoreboard appears with all ten names at once.
    These are the frames that returned nine names in one go.
  * a few seconds after any kill, while the killfeed still shows who killed whom.

Round ends are tried first because they are worth far more per frame. We stop
as soon as enough distinct names have come back to make the answer certain,
which on a real streamer is usually the first or second frame.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A round ends a beat after its final kill; the scoreboard then sits on screen
# for several seconds. Five is comfortably inside that window even when the
# VOD's clock and the match clock disagree by a second or two.
ROUND_END_LEAD_S = 5.0
KILLFEED_LEAD_S = 2.0
# Enough distinct names that no other match could explain them -- counted after
# the candidate's own name is dropped, so every one of these is independent of
# the reason we were looking at this channel.
#
# Four, not five, and the difference was measured rather than guessed. Two
# things make a small number decisive here. The broadcast already overlaps this
# match's wall-clock, and at that moment these ten people were in this match, so
# seeing four of them on screen means the broadcaster was in it too. And across
# forty frames of broadcasts that were definitely other games, the number of
# independent lobby names returned was zero -- every apparent hit was the
# channel's own branding, which is now excluded.
#
# Five cost real detections: two confirmed streamers in high-elo lobbies rested
# on exactly four independent names apiece, with no clip and no bio to fall back
# on. Four keeps them without admitting anything a coincidence could produce.
CERTAIN_NAMES = 4
MAX_FRAMES = 10


def _normalise_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


@dataclass
class Watched:
    """What looking at the broadcast actually told us."""

    frames_read: int = 0
    frames_failed: int = 0
    valorant_frames: int = 0
    timer_frames: int = 0
    names: set[str] = field(default_factory=set)
    me_seen: bool = False
    unavailable: str = ""

    @property
    def certain(self) -> bool:
        return len(self.names) >= CERTAIN_NAMES

    @property
    def saw_nothing(self) -> bool:
        """We looked properly and the match was simply not there.

        Deliberately ignores `timer_frames`: a timer at the top of the screen is
        not Valorant's alone -- Deadlock draws one within pixels of the same
        place -- so counting it here would leave us unable to rule anything out.
        """
        return self.frames_read >= 3 and not self.names and self.valorant_frames == 0


def sample_offsets(kills: list[tuple[int, float]], *, limit: int = MAX_FRAMES,
                   window: tuple[float, float] | None = None) -> list[float]:
    """Seconds into the match worth sampling, best first.

    `kills` is (round, seconds_into_match), in time order. `window` restricts
    sampling to the stretch of the match a broadcast actually covers: a stream
    that joined twenty minutes late still holds every later round, and the
    frames it does have are as good as anyone's.
    """
    if not kills:
        return []
    if window is not None:
        lo, hi = window
        kills = [(r, t) for r, t in kills if lo <= t <= hi]
        if not kills:
            return []
    last_of_round: dict[int, float] = {}
    for rnd, at in kills:
        last_of_round[rnd] = max(last_of_round.get(rnd, 0.0), at)

    # Round ends, spread over the whole match rather than clustered at the start.
    ends = [t + ROUND_END_LEAD_S for _, t in sorted(last_of_round.items())]
    ends = _spread(ends, limit)

    feed = _spread([at + KILLFEED_LEAD_S for _, at in kills], limit)

    out: list[float] = []
    for group in (ends, feed):
        for t in group:
            if all(abs(t - seen) > 4.0 for seen in out):
                out.append(t)
    return out[:limit]


def _spread(values: list[float], count: int) -> list[float]:
    """`count` values drawn evenly across the list, order preserved."""
    if len(values) <= count:
        return list(values)
    step = len(values) / count
    return [values[min(len(values) - 1, int(i * step))] for i in range(count)]


def watch(reader, frames, vod_id: str, vod_offset_s: float,
          kills: list[tuple[int, float]], names: dict[str, str],
          my_name: str = "", *, limit: int = MAX_FRAMES,
          window: tuple[float, float] | None = None,
          exclude_name: str = "") -> Watched:
    """Look at a broadcast and report which lobby members appear in it.

    `vod_offset_s` is where the match starts inside the VOD: the match's start
    minus the broadcast's start.

    `exclude_name` is the player this channel is a candidate *for*, and it is
    dropped from the search. This matters more than it sounds. A channel
    becomes a candidate because its name resembles that player's, so its own
    branding - the watermark, the overlay, the stream's own title card - names
    something very like the player by construction. Measured on four
    broadcasts we had correctly ruled out, every one returned exactly one
    "lobby name": twitch.tv/curry showing CURRY, twitch.tv/rogue showing
    Rogue, sacred showing their Apex name sacred_NRG. That is the candidate
    agreeing with itself, not evidence about the match, and counting it would
    put every name-matched channel one weak step from confirmation.

    The other nine players carry no such circularity: nothing about this
    channel predicts their names appearing on it.
    """
    if exclude_name:
        key = _normalise_key(exclude_name)
        names = {k: v for k, v in names.items() if k != key}
    seen = Watched()
    if not reader.available:
        seen.unavailable = "no OCR engine (pip install clipsvalorant[vision])"
        return seen
    if not names:
        seen.unavailable = "no lobby name long enough to search for"
        return seen

    for at in sample_offsets(kills, limit=limit, window=window):
        path = frames.frame(vod_id, vod_offset_s + at)
        if path is None:
            seen.frames_failed += 1
            # A VOD with no playlist at all will never produce a frame; do not
            # spend the whole budget discovering that ten times over.
            if seen.frames_failed >= 2 and seen.frames_read == 0:
                break
            continue
        read = reader.read(path)
        if read is None:
            seen.frames_failed += 1
            continue
        seen.frames_read += 1
        if read.looks_like_valorant:
            seen.valorant_frames += 1
        if read.has_match_timer:
            seen.timer_frames += 1
        hits = read.names_present(names)
        seen.names |= hits
        if my_name and my_name in hits:
            seen.me_seen = True
        if seen.certain:
            break
    return seen
