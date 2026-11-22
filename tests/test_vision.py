"""Layer 5: what the broadcast itself shows.

Every layer before this one builds a case out of circumstance - a name that
matches, a title that mentions the game, a stream that was running at the right
time. That case was wrong about Curry and it was silent about CocaPup, which is
the point: circumstance cuts both ways.

Valorant paints the lobby's names on the screen all match long. We know all ten
from the match history, so the question "was this broadcast showing our match?"
has a direct answer that owes nothing to inference. These tests pin down the
rules for reading it, and for what to do when the answer is no.
"""

from clipsvalorant.resolve import Evidence
import json
from pathlib import Path

from clipsvalorant import vision
from clipsvalorant.vision import FrameRead, TextBox, normalise, wanted_names
from clipsvalorant.watch import (
    CERTAIN_NAMES, KILLFEED_FIRST, MAX_FRAMES, Watched, end_anchors,
    sample_offsets, watch,
)


def _frame(*texts: tuple[str, float, float], size: tuple[int, int] = (1280, 720)) -> FrameRead:
    boxes = tuple(TextBox(t, x, y, x + 50, y + 20) for t, x, y in texts)
    return FrameRead(boxes, size[0], size[1])


# --------------------------------------------------------------- reading names

def test_a_name_is_found_however_the_ocr_mangles_it():
    """The combat report glues the player's title onto their name, and the OCR
    then drops or invents a character at the join. Both of these came back from
    real frames for a single on-screen name."""
    names = wanted_names([("PlayerOne", True)])
    assert _frame(("TAG(PlayerOne", 1010, 306)).names_present(names) == {"PlayerOne"}
    assert _frame(("AGIPlayerOne", 1015, 354)).names_present(names) == {"PlayerOne"}


def test_a_name_too_short_is_never_searched_for():
    """A three-character name matches inside half the words on a busy screen -
    'ave' is in 'average', 'have', 'behaved'. Such names are dropped before we
    look, rather than explained away afterwards."""
    assert wanted_names([("ave", False)]) == {}
    assert wanted_names([("PlayerTwo", False)]) == {"playertwo": "PlayerTwo"}


def test_names_come_back_in_their_original_spelling():
    names = wanted_names([("two words", False)])
    assert _frame(("twowords", 10, 10)).names_present(names) == {"two words"}


# ------------------------------------------------------- telling games apart

def test_valorant_furniture_is_what_identifies_valorant():
    assert _frame(("COMBAT REPORT", 1095, 375)).looks_like_valorant
    assert _frame(("BUY PHASE", 555, 109)).looks_like_valorant
    assert not _frame(("DASH-JUMP: TOO LATE", 576, 587)).looks_like_valorant


def test_a_timer_at_the_top_is_not_enough_to_mean_valorant():
    """Deadlock draws a match timer within pixels of Valorant's. A frame that
    has only that is a frame we know nothing about."""
    deadlock = _frame(("7:05", 628, 9), ("$641 SOULS", 56, 590))
    assert deadlock.has_match_timer
    assert not deadlock.looks_like_valorant


def test_a_timer_somewhere_else_on_screen_is_not_a_match_timer():
    assert not _frame(("0:19", 20, 600)).has_match_timer
    assert _frame(("0:19", 614, 18)).has_match_timer


# ------------------------------------------------------- choosing the moments

def test_round_ends_are_sampled_before_mid_round_kills():
    """The round-end scoreboard shows all ten names at once; a killfeed shows
    two. Spend the first frame where it buys the most."""
    kills = [(1, 30.0), (1, 45.0), (2, 90.0), (2, 110.0)]
    first = sample_offsets(kills, limit=4)[0]
    assert first == 50.0  # last kill of round 1, plus the scoreboard lead


def test_sampling_spreads_over_the_whole_match():
    kills = [(r, r * 60.0 + i) for r in range(1, 25) for i in (0.0, 5.0)]
    picked = sample_offsets(kills, limit=6)
    assert len(picked) == 6
    assert max(picked) - min(picked) > 600


def test_sampling_is_confined_to_what_the_broadcast_covers():
    """A stream that joined twenty minutes in has no frames of the first round.
    Spending the budget seeking to them wastes the whole budget."""
    kills = [(1, 30.0), (5, 600.0), (9, 1200.0), (12, 1500.0)]
    picked = sample_offsets(kills, limit=6, window=(900.0, 1600.0))
    assert picked
    assert all(900.0 <= t <= 1610.0 for t in picked)


def test_a_window_holding_none_of_the_action_yields_nothing():
    assert sample_offsets([(1, 30.0), (2, 90.0)], window=(900.0, 1600.0)) == []


def test_no_kills_means_nowhere_worth_looking():
    assert sample_offsets([]) == []


def test_the_post_game_scoreboard_is_looked_for_past_the_end_of_the_match():
    """It is the one screen that lists the whole lobby with no ceiling on how
    much of it can be read: twitch.tv/henkyyyval returned nine of nine names
    off a single frame thirty seconds after the final round."""
    assert end_anchors(1800.0, 300.0) == [1825.0, 1845.0, 1810.0, 1875.0]


def test_a_broadcast_that_stopped_on_the_final_round_offers_no_scoreboard():
    """Asking for a frame past the end of the video spends a fetch to be told
    there isn't one, and the budget is in frames, not in useful frames."""
    assert end_anchors(1800.0, 0.0) == []
    assert end_anchors(0.0, 300.0) == []


def test_only_the_scoreboard_moments_the_broadcast_actually_reaches_are_used():
    assert end_anchors(1800.0, 30.0) == [1825.0, 1810.0]


# ------------------------------------------- spending the budget in the right order

def _driver(on_screen: dict[int, list[str]], dead: tuple[int, ...] = ()):
    """A reader and a frame store standing in for a real broadcast.

    `on_screen` says which names a given second of the broadcast shows, and
    `dead` which seconds cannot be fetched at all.
    """
    asked: list[float] = []

    class _Frames:
        def iter_frames(self, vod_id, offsets, ahead=4):
            for offset in offsets:
                asked.append(offset)
                yield offset, (None if int(offset) in dead
                               else Path(f"{int(offset)}.png"))

    class _Reader:
        available = True

        def read(self, path):
            second = int(Path(path).stem)
            return _frame(*[(name, 10.0, 300.0)
                            for name in on_screen.get(second, ())])

    return _Reader(), _Frames(), asked


# Round ends land on a hundred-second grid, so the killfeed frames are 105,
# 205 ... 1005 and the match ends at 1100 with a hundred seconds of broadcast
# left after it.
_KILLS = [(rnd, rnd * 100.0) for rnd in range(1, 11)]
_TAIL = {"match_end_s": 1100.0, "tail_s": 100.0}


def test_the_scoreboard_is_tried_once_the_killfeed_has_stopped_paying():
    """Three killfeed frames showing nothing is the signal that this broadcast
    does not read well, and the post-game scoreboard is the better bet from
    there. Measured over the 31 confirmed and likely encounters on disk, this
    settles 17 against 14 and reads 185 frames against 202."""
    reader, frames, asked = _driver({1125: ["alpha", "bravo", "charlie", "delta"]})
    seen = watch(reader, frames, "v1", 0.0, _KILLS,
                 wanted_names([(n, False) for n in
                               ("alpha", "bravo", "charlie", "delta")]), **_TAIL)
    assert seen.certain
    assert 1125.0 in asked
    assert asked.index(1125.0) == KILLFEED_FIRST  # straight after the three


def test_a_killfeed_that_is_nearly_there_is_not_interrupted():
    """twitch.tv/killerjohnson77 had three names by its second frame, the
    fourth not until its ninth, and nothing at all on any scoreboard frame.
    Three names in hand means the broadcast reads well, so its own frames are
    worth more than a screen that may not be there."""
    names = wanted_names([(n, False) for n in
                          ("alpha", "bravo", "charlie", "delta")])
    reader, frames, asked = _driver({
        105: ["alpha"], 205: ["bravo"], 305: ["charlie"],
        905: ["delta"], 1125: ["alpha", "bravo", "charlie", "delta"],
    })
    seen = watch(reader, frames, "v1", 0.0, _KILLS, names, **_TAIL)
    assert seen.certain
    assert 1125.0 not in asked, "the scoreboard crowded out the frame that settled it"


def test_a_rejected_candidate_is_never_charged_for_the_scoreboard():
    """The anchors displace killfeed frames, they are never spent on top of
    them. Nearly every candidate is the wrong person, so this is the case that
    decides whether a scan got slower, and it must cost exactly what it did."""
    names = wanted_names([(n, False) for n in ("alpha", "bravo", "charlie", "delta")])
    reader, frames, asked = _driver({})
    seen = watch(reader, frames, "v1", 0.0, _KILLS, names, **_TAIL)
    assert not seen.certain
    assert seen.frames_read == MAX_FRAMES
    assert len(asked) == MAX_FRAMES


def test_a_broadcast_with_no_video_is_abandoned_not_sampled_to_the_end():
    """Two dead frames and nothing read means there is no playlist to seek
    into, and the remaining eight fetches will all fail the same way."""
    names = wanted_names([(n, False) for n in ("alpha", "bravo", "charlie", "delta")])
    reader, frames, asked = _driver({}, dead=tuple(range(0, 2000)))
    seen = watch(reader, frames, "v1", 0.0, _KILLS, names, **_TAIL)
    assert seen.frames_read == 0
    assert seen.frames_failed == 2


# -------------------------------------------------------- ruling things out

def test_enough_of_the_lobby_on_screen_is_a_confirmation():
    ev = Evidence(vod_covers=True,
                  lobby_names_seen=tuple(str(i) for i in range(CERTAIN_NAMES)))
    assert ev.vod_shows_this_match
    assert ev.verdict() == "confirmed"


def test_a_couple_of_names_could_still_be_the_same_friend_group():
    """The same duo partners turn up across many of your matches, so two or
    three shared names prove nothing on their own."""
    ev = Evidence(vod_covers=True, lobby_names_seen=("PlayerOne", "PlayerTwo"))
    assert not ev.vod_shows_this_match


def test_looking_and_finding_nothing_outranks_a_perfect_circumstantial_case():
    """This is CocaPup's evidence exactly - the name fits, the bio names them,
    the broadcast is Valorant - and it would have been confirmed on that alone.
    Having actually watched the VOD and found no trace of the match, we don't."""
    ev = Evidence(vod_covers=True, name_prefix=True, bio_names_player=True,
                  title_says_valorant=True, frames_read=10,
                  vod_not_this_match=True)
    assert ev.verdict() == "rejected"


def test_a_stream_that_joined_late_is_still_confirmed_by_what_is_on_screen():
    """Coverage is a guess about what a broadcast contains; reading it is a
    measurement. A stream holding a third of the match fails the coverage
    test and can still plainly show the lobby, so the measurement wins."""
    ev = Evidence(vod_covers=False, vod_overlap_s=600, match_len_s=1800,
                  lobby_names_seen=tuple(str(i) for i in range(CERTAIN_NAMES)))
    assert not ev.vod_usable
    assert ev.verdict() == "confirmed"


def test_thin_coverage_with_nothing_on_screen_is_still_rejected():
    ev = Evidence(vod_covers=False, vod_overlap_s=600, match_len_s=1800)
    assert ev.verdict() == "rejected"


def test_a_vod_we_could_not_open_changes_nothing():
    """No OCR engine, an expired VOD, a subscriber-only broadcast: Layer 5 goes
    quiet and the layers above it decide as they did before - which is to say
    they reach 'likely' and stop, because none of them can tell this player
    from a stranger with a similar name who was also playing Valorant."""
    ev = Evidence(vod_covers=True, name_prefix=True, bio_names_player=True,
                  title_says_valorant=True, frames_read=0)
    assert ev.verdict() == "likely"


def test_a_clip_with_no_tie_to_the_player_is_not_even_a_candidate():
    """A Valorant clip from a covering broadcast and nothing linking the
    channel to this player is a description of every Valorant streamer who was
    live that evening."""
    ev = Evidence(vod_covers=True, clips_inside_match=2, frames_read=0)
    assert ev.verdict() == "unconfirmed"


def test_seeing_the_lobby_counts_as_tying_to_this_match():
    ev = Evidence(vod_covers=True,
                  lobby_names_seen=tuple(str(i) for i in range(CERTAIN_NAMES)))
    assert ev.ties_to_this_match
    assert "appear by name in the VOD" in (ev.confirmed_by() or "")


# ---------------------------------------------- the candidate's own name

def test_the_candidates_own_name_is_not_evidence_about_the_match():
    """A channel is a candidate because its name resembles the player's, so its
    own watermark names something very like that player by construction.

    Measured on four broadcasts we had correctly ruled out, every single one
    returned exactly one "lobby name" this way: twitch.tv/curry showing CURRY,
    twitch.tv/rogue showing Rogue, sacred showing their Apex name sacred_NRG.
    That is the candidate agreeing with itself. Counting it would put every
    name-matched channel one weak step away from being confirmed.
    """
    from clipsvalorant.watch import _normalise_key
    lobby = wanted_names([("Curry", False), ("Juneha", False), ("CREED", False)])
    kept = {k: v for k, v in lobby.items() if k != _normalise_key("Curry")}
    assert "curry" not in kept
    assert _frame(("CURRYO", 50, 50)).names_present(kept) == set()
    # the other players carry no such circularity
    assert _frame(("Juneha", 50, 50)).names_present(kept) == {"Juneha"}


def test_excluding_one_name_leaves_a_real_confirmation_standing():
    """CocaPup's broadcast returned all ten lobby names. Dropping their own
    still leaves nine, which is the point: real evidence does not depend on
    the one name that was never independent."""
    seen = {"CocaPup", "Georgy", "Nuke", "Razer", "Scarecrow",
            "heyyharvey", "resticle", "Bigger", "Inferno"}
    assert len(seen - {"CocaPup"}) >= CERTAIN_NAMES


# ------------------------------------------------------------ the watch result

def test_a_few_frames_are_not_enough_to_rule_anything_out():
    assert not Watched(frames_read=2).saw_nothing


def test_a_timer_alone_does_not_save_a_broadcast_from_being_ruled_out():
    """Curry's VOD: ten frames read, a timer in nine of them, and not one
    Valorant word or lobby name. That is Deadlock, and we can say so."""
    assert Watched(frames_read=10, timer_frames=9, valorant_frames=0).saw_nothing


def test_valorant_on_screen_is_not_the_absence_curry_was():
    """Two different findings, and only one of them is "nothing was there".

    Curry's broadcast had no Valorant in it at all, which is what `saw_nothing`
    means. A broadcast full of Valorant with none of this lobby in it is the
    opposite kind of answer - a positive reading of somebody else's match - and
    conflating the two is how NeonQuin survived being looked at.
    """
    seen = Watched(frames_read=10, valorant_frames=4)
    assert not seen.saw_nothing
    assert seen.other_valorant


def test_a_different_game_of_valorant_is_something_we_can_say():
    """Measured on twitch.tv/NeonQuin against yrn#wkmts's Lotus match of
    18 November: ten frames read, seven of them unmistakably Valorant, zero of
    the eight readable lobby names. Pulled by hand, the frame the page linked
    to showed ROUND 3 at 0-2 with five agents from a different match."""
    assert Watched(frames_read=10, valorant_frames=7).other_valorant


def test_a_glance_is_not_enough_to_call_it_somebody_elses_match():
    """Two frames of Valorant and no names is a stream we barely looked at."""
    assert not Watched(frames_read=4, valorant_frames=4).other_valorant
    assert not Watched(frames_read=10, valorant_frames=2).other_valorant
    assert not Watched(frames_read=10, valorant_frames=9,
                       names={"Ally1"}).other_valorant


def test_certainty_needs_the_whole_threshold():
    assert Watched(names=set(str(i) for i in range(CERTAIN_NAMES))).certain
    assert not Watched(names=set(str(i) for i in range(CERTAIN_NAMES - 1))).certain


def test_an_unreadable_frame_is_never_an_error(tmp_path):
    """Failing to read a frame is the commonest outcome in the whole program -
    expired VODs, subscriber-only broadcasts, a seek past the end. It happened
    to crash a run instead: on Windows the file ffmpeg abandoned was still held
    open, and deleting it raised PermissionError out of the frame reader."""
    from clipsvalorant.vod import FrameReader
    reader = FrameReader(tmp_path)
    empty = tmp_path / "held.png"
    empty.write_bytes(b"too small")
    assert FrameReader._keep_if_real(empty) is None
    assert FrameReader._keep_if_real(tmp_path / "never_written.png") is None


def test_a_real_frame_survives_the_check(tmp_path):
    from clipsvalorant.vod import FrameReader
    good = tmp_path / "good.png"
    good.write_bytes(b"x" * 2048)
    assert FrameReader._keep_if_real(good) == good


def test_being_live_on_valorant_is_a_game_check_a_title_cannot_fake():
    """One broadcast in the sample was titled "VCT OQ Watchparty" while showing
    Deadlock. /helix/streams reports the game id itself, so for a match that has
    only just ended it answers the question the title was guessing at."""
    ev = Evidence(vod_covers=True, name_prefix=True, live_on_valorant=True)
    assert ev.looks_like_valorant
    assert ev.verdict() == "likely"


def test_being_live_is_still_not_evidence_about_this_match():
    """Live on Valorant now says nothing about who was in that lobby an hour
    ago, so on its own it never confirms."""
    ev = Evidence(vod_covers=True, live_on_valorant=True)
    assert ev.verdict() == "unconfirmed"


def test_normalise_strips_what_ocr_invents():
    assert normalise("TAG | PlayerOne") == "tagplayerone"
    assert normalise("COMBAT REPORT") == "combatreport"


# ------------------------------------------------- the pass over a broadcast

class _Reader:
    """An OCR engine that returns a prepared frame per offset."""

    def __init__(self, screens: dict[int, FrameRead], available: bool = True):
        self.screens = screens
        self.available = available
        self.read_paths: list[str] = []

    def read(self, path):
        self.read_paths.append(str(path))
        return self.screens.get(int(str(path)))


class _Frames:
    """Stands in for FrameReader. Records the order offsets were asked for."""

    def __init__(self, missing=()):
        self.missing = set(missing)
        self.asked: list[int] = []

    def frame(self, vod_id, offset):
        self.asked.append(int(offset))
        return None if int(offset) in self.missing else str(int(offset))

    def iter_frames(self, vod_id, offsets, *, ahead=4):
        for offset in offsets:
            yield offset, self.frame(vod_id, offset)


# Ten seconds apart, one round each, so sample_offsets lands predictably.
KILLS = [(r, 100.0 + r * 60.0) for r in range(8)]
LOBBY = wanted_names([(f"Player{n}", False) for n in "ABCDEFGH"])


def _named(*names: str) -> FrameRead:
    return _frame(*[(n, 600, 300 + i * 20) for i, n in enumerate(names)])


def test_watch_stops_as_soon_as_the_answer_is_certain():
    """Four independent names settle it, and every further frame is spend on a
    question already answered - about three seconds each, on a channel that is
    obviously the right one."""
    from clipsvalorant.watch import watch

    screens = {}
    for off in [int(100.0 + r * 60.0 + 5.0) for r in range(8)]:
        screens[off] = _named("PlayerA", "PlayerB", "PlayerC", "PlayerD")
    frames = _Frames()
    reader = _Reader(screens)
    seen = watch(reader, frames, "v1", 0.0, KILLS, LOBBY, limit=10)

    assert seen.certain
    assert seen.frames_read == 1
    assert len(frames.asked) < 10  # the rest were never fetched


def test_watch_gives_up_on_a_broadcast_that_yields_no_frames():
    """A VOD past Twitch's retention has no playlist, so every frame fails.
    Discovering that ten times over is the commonest way to waste a sweep."""
    from clipsvalorant.watch import watch

    frames = _Frames(missing=range(0, 100_000))
    seen = watch(_Reader({}), frames, "v1", 0.0, KILLS, LOBBY, limit=10)

    assert seen.frames_read == 0
    assert seen.frames_failed == 2  # stopped, rather than spending the budget
    assert not seen.certain


def test_watch_keeps_what_it_read_for_calibration_to_reuse():
    """Confirming the streamer and timing the match are one act with two
    questions asked of it. Paying twice to read one broadcast is what made a
    scan slow."""
    from clipsvalorant.watch import watch

    first = int(100.0 + 5.0)
    screens = {first: _named("PlayerA", "PlayerB")}
    frames = _Frames()
    seen = watch(_Reader(screens), frames, "v1", 0.0, KILLS, LOBBY, limit=3)

    assert [off for off, _ in seen.reads] == [float(first)]
    assert seen.reads[0][1] is screens[first]


def test_watch_drops_the_candidates_own_name_before_looking():
    """A channel is a candidate because its name resembles the player's, so
    its own branding names something very like them by construction."""
    from clipsvalorant.watch import watch

    off = int(100.0 + 5.0)
    screens = {off: _named("PlayerA", "PlayerB")}
    seen = watch(_Reader(screens), _Frames(), "v1", 0.0, KILLS, LOBBY,
                 limit=1, exclude_name="PlayerA")
    assert seen.names == {"PlayerB"}


def test_watch_without_an_ocr_engine_says_so_rather_than_ruling_out():
    from clipsvalorant.watch import watch

    seen = watch(_Reader({}, available=False), _Frames(), "v1", 0.0, KILLS, LOBBY)
    assert seen.unavailable and seen.frames_read == 0
    assert not seen.saw_nothing  # we did not look, so we concluded nothing


def test_watch_asks_for_offsets_in_the_order_it_wants_them():
    """Prefetching must not reorder: round ends are tried before killfeed
    frames because they are worth far more per frame, and an early exit on a
    reordered list would spend the budget on the wrong ones."""
    from clipsvalorant.watch import watch

    frames = _Frames()
    watch(_Reader({}), frames, "v1", 0.0, KILLS, LOBBY, limit=6)
    assert frames.asked == sorted(frames.asked, key=lambda o: frames.asked.index(o))
    assert frames.asked == [int(o) for o in sample_offsets(KILLS, limit=6)]


# ------------------------------------------------- frames remembered on disk

class _CountingReader(vision.Reader):
    """A Reader with a scripted engine, counting how often it actually runs."""

    def __init__(self, boxes=(("CURRY", 10, 10, 60, 24),), fails=False):
        super().__init__()
        self._tried = True
        self._engine = object()      # merely has to be non-None
        self.runs = 0
        self._boxes = boxes
        self._fails = fails

    def _read_uncached(self, path):
        self.runs += 1
        if self._fails:
            return None
        return vision.FrameRead(
            tuple(vision.TextBox(t, x0, y0, x1, y1)
                  for t, x0, y0, x1, y1 in self._boxes),
            1280, 720,
        )


def _png(tmp_path, name="f.png"):
    p = tmp_path / name
    p.write_bytes(b"not really a png")
    return p


def test_a_frame_read_once_is_not_read_again_by_a_later_run(tmp_path):
    """The reason this exists. OCR is about a second and a quarter of CPU per
    frame, it does not thread and no engine setting makes it cheaper, so the
    only saving left is not doing it twice - which is most of what a sweep
    over the same fortnight was spending its time on."""
    frame = _png(tmp_path)
    first = _CountingReader()
    assert first.read(frame) is not None
    assert first.runs == 1

    later = _CountingReader()          # a whole new process, as a sweep is
    got = later.read(frame)
    assert later.runs == 0, "the frame was read from scratch a second time"
    assert got is not None
    assert got.blob == "curry"
    assert (got.width, got.height) == (1280, 720)


def test_what_comes_back_from_disk_is_what_went_in(tmp_path):
    """A verdict rests on box positions as well as text: the match timer is
    only a timer because of where on the screen it sits."""
    boxes = (("1:34", 600.5, 12.0, 648.0, 40.5), ("CURRY", 10, 10, 60, 24))
    frame = _png(tmp_path)
    _CountingReader(boxes).read(frame)
    got = _CountingReader(boxes).read(frame)
    assert [(b.text, b.x0, b.y0, b.x1, b.y1) for b in got.boxes] == [
        ("1:34", 600.5, 12.0, 648.0, 40.5), ("CURRY", 10.0, 10.0, 60.0, 24.0)
    ]
    assert got.has_match_timer


def test_a_frame_that_could_not_be_read_is_not_remembered_as_empty(tmp_path):
    """A half-written frame or a busy engine is a passing problem. Writing it
    down would turn it into a permanent one, and an empty read is exactly what
    `saw_nothing` treats as evidence of absence."""
    frame = _png(tmp_path)
    broken = _CountingReader(fails=True)
    assert broken.read(frame) is None

    working = _CountingReader()
    assert working.read(frame) is not None
    assert working.runs == 1, "the failure was cached and never retried"


def test_a_note_from_an_older_format_is_ignored(tmp_path):
    """Should the engine or its settings ever change, every note on disk is
    stale. The version is what lets that be a re-read rather than a wrong
    answer served forever."""
    frame = _png(tmp_path)
    _CountingReader().read(frame)
    note = vision._sidecar(frame)
    blob = json.loads(note.read_text(encoding="utf-8"))
    blob["v"] = vision.OCR_SIDECAR_VERSION + 1
    note.write_text(json.dumps(blob), encoding="utf-8")

    again = _CountingReader()
    assert again.read(frame) is not None
    assert again.runs == 1, "a note from another version was trusted"


def test_a_damaged_note_is_ignored_rather_than_fatal(tmp_path):
    frame = _png(tmp_path)
    _CountingReader().read(frame)
    vision._sidecar(frame).write_text("{ this is not json", encoding="utf-8")
    r = _CountingReader()
    assert r.read(frame) is not None
    assert r.runs == 1


def test_a_read_still_works_where_nothing_can_be_written(tmp_path, monkeypatch):
    """The cache is a shortcut, never a requirement."""
    frame = _png(tmp_path)

    def no(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(vision.Path, "write_text", no)
    r = _CountingReader()
    assert r.read(frame) is not None
    assert r.runs == 1
