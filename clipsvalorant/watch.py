"""Choosing which seconds of a broadcast to look at.

Opening a VOD is cheap but not free -- roughly three seconds per frame, between
the seek and the OCR -- so the order in which we sample matters more than the
number of frames we are willing to spend.

Three kinds of moment put names on screen, and we know when all three happen
because the match history gives us every kill to the millisecond and the
match's own length:

  * the end of a round, when the scoreboard appears with all ten names at once.
    These are the frames that returned nine names in one go.
  * a few seconds after any kill, while the killfeed still shows who killed whom.
  * the half-minute after the match ends, when the post-game scoreboard carries
    the whole lobby with its scores.

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
# What counts as having looked, before "no lobby name here" is allowed to mean
# "this is somebody else's match". Half the frame budget, of which most showed
# Valorant's own furniture rather than a loading screen or a menu.
ENOUGH_FRAMES = 5
ENOUGH_VALORANT = 3

# Seconds past the end of the match, where the post-game scoreboard sits. It
# lists every player with their score, so one readable frame here answers the
# whole question - twitch.tv/henkyyyval returned nine of nine names off a
# single one. Ordered by how often each paid off over the 31 confirmed and
# likely encounters on disk rather than by time.
#
# The obvious cousin of this idea does not work and was measured before being
# abandoned: aiming *before* the match, at the load-in screen that shows all
# ten players with their agents. Agent select hides the enemy team, so a frame
# that lands early can never carry more than four names, and half of them
# landed on agent select or on a tracker overlay the streamer had alt-tabbed
# into. The post-game screen has no such ceiling.
END_ANCHOR_AT = (25.0, 45.0, 10.0, 75.0)

# The killfeed goes first and the anchors only displace it when it is not
# working. Three independent names already in hand means this broadcast reads
# well and one more of its own frames is likely to finish the job -- which is
# exactly the candidate a scoreboard frame would have crowded out.
# twitch.tv/killerjohnson77 is the case: three names by its second frame, the
# fourth not until its ninth, and no anchor frame carrying anything. Measured
# over the 31 confirmed and likely encounters on disk, this ordering settles
# 17 against the 14 the killfeed settles alone, reads 185 frames against 202,
# and loses none of the ones already being found.
KILLFEED_FIRST = 3
ENOUGH_TO_STAY = 3


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
    # Every frame this pass actually parsed, as (offset in the broadcast, the
    # read). Calibration wants the round countdown off frames like these, and
    # it used to pay for a second pass of its own to get them. The OCR is
    # already done here; handing it on is free.
    reads: list[tuple[float, object]] = field(default_factory=list)

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

    @property
    def other_valorant(self) -> bool:
        """Valorant was plainly on screen, and none of this lobby was in it.

        This used to be the one thing we refused to conclude -- "maybe an
        overlay, maybe the streamer was in a menu" -- and that caution cost the
        single worst false positive the project has produced. twitch.tv/NeonQuin
        was reported as NeoN#Rage in yrn#wkmts's Lotus match of 18 November on
        the strength of a Valorant clip inside the window. Ten frames were read
        at the time, five of them unmistakably Valorant, and not one of the
        eight readable lobby names appeared. Pulled by hand afterwards, the
        frame the page linked to showed ROUND 3, 0-2, with five agents none of
        which were in our match: a different game, started twelve minutes later.

        Zero names out of eight across five Valorant frames is not a thin
        reading. The two broadcasts confirmed by other means returned nine and
        ten names on their first frame, and the ones that were other matches
        returned none -- there is nothing in between to be careful about.
        """
        return (
            self.frames_read >= ENOUGH_FRAMES
            and self.valorant_frames >= ENOUGH_VALORANT
            and not self.names
        )


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


def end_anchors(match_end_s: float, tail_s: float) -> list[float]:
    """The post-game moments this broadcast actually reaches.

    `match_end_s` is the length of the match on the in-match clock, so these
    come back in the same units as `sample_offsets` and the caller adds the
    same offset to both. `tail_s` is how much broadcast remains after the
    match ends -- a stream that stopped on the final round has none of this to
    offer, and asking for a frame past the end of the video spends a fetch to
    be told so.
    """
    if match_end_s <= 0 or tail_s <= 0:
        return []
    return [match_end_s + at for at in END_ANCHOR_AT if at <= tail_s]


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
          exclude_name: str = "", match_end_s: float = 0.0,
          tail_s: float = 0.0) -> Watched:
    """Look at a broadcast and report which lobby members appear in it.

    `vod_offset_s` is where the match starts inside the VOD: the match's start
    minus the broadcast's start.

    `match_end_s` and `tail_s` open up the post-game scoreboard; without them
    only the killfeed and the round ends are sampled, which is what every
    caller did before `end_anchors` existed.

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

    def look(offsets: list[float]) -> bool:
        """Read these moments. True when the answer is in and we should stop."""
        for offset, path in frames.iter_frames(vod_id, offsets):
            if path is None:
                seen.frames_failed += 1
                # A VOD with no playlist at all will never produce a frame; do
                # not spend the whole budget discovering that ten times over.
                if seen.frames_failed >= 2 and seen.frames_read == 0:
                    return True
            else:
                read = reader.read(path)
                if read is None:
                    seen.frames_failed += 1
                else:
                    seen.frames_read += 1
                    seen.reads.append((offset, read))
                    if read.looks_like_valorant:
                        seen.valorant_frames += 1
                    if read.has_match_timer:
                        seen.timer_frames += 1
                    hits = read.names_present(names)
                    seen.names |= hits
                    if my_name and my_name in hits:
                        seen.me_seen = True
                    if seen.certain or seen.other_valorant:
                        # Both are answers, and the second one is the commoner
                        # by far: most candidates are the wrong person, and
                        # reading five more frames of somebody else's match
                        # cannot change that. Across one lobby of razor#d4c's
                        # that is the difference between ten frames per
                        # rejected candidate and five, on the single slowest
                        # thing a scan does.
                        return True
            # The budget is frames attempted, not frames read, and it is the
            # whole budget rather than one per group -- the anchors displace
            # killfeed frames, they are never spent on top of them. A rejected
            # candidate therefore costs exactly what it cost before, which is
            # the case that decides whether a scan got slower.
            if seen.frames_read + seen.frames_failed >= limit:
                return True
        return False

    killfeed = sample_offsets(kills, limit=limit, window=window)
    if look([vod_offset_s + at for at in killfeed[:KILLFEED_FIRST]]):
        return seen
    rest = killfeed[KILLFEED_FIRST:]
    if len(seen.names) < ENOUGH_TO_STAY:
        rest = end_anchors(match_end_s, tail_s) + rest
    look([vod_offset_s + at for at in rest])
    return seen
