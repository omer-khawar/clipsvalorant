"""Where a match really sits inside a broadcast.

This is the arithmetic that was wrong for the whole life of the project, and
wrong in the way that is hardest to notice: the link opened the right
broadcast, on the right map, in the right match, about a round and a half
before the thing it promised. Nothing errored and nothing looked broken.

The cause is that `metadata.started_at` and `time_in_match_in_ms` do not
share a zero. The timestamp is the queue popping; the clock starts when the
map finishes loading. Agent select and loading sit in between, and they are
not a constant - 101, 109, 125 and 79 seconds across the four broadcasts
measured so far.

So the tests below are mostly about the measurement being *sceptical*: one
frame may never be enough, a timer somewhere other than the top centre is
not the round timer, and an answer outside the plausible range is no answer.
"""

import pytest

from clipsvalorant import calibrate, db
from clipsvalorant.vision import FrameRead, TextBox

# Round starts from a real match (Corrode, vaerie's broadcast). Irregular, as
# real rounds are, which is exactly why a shift one round out cannot fit.
STARTS = {0: 55.1, 1: 140.3, 2: 214.2, 3: 304.3, 4: 393.9, 5: 455.6, 6: 517.6}
TRUE_SHIFT = 101.0


def frame(boxes, width=1280, height=720):
    return FrameRead(tuple(boxes), width, height)


def timer_box(text, *, cx=0.5, top=0.03, width=1280, height=720):
    """A text box placed by fraction of the frame, as OCR would return it."""
    half = 0.02 * width
    return TextBox(text, cx * width - half, top * height,
                   cx * width + half, (top + 0.03) * height)


# ------------------------------------------------------------ round starts

def test_round_starts_come_from_the_two_clocks_on_a_kill():
    kills = [
        {"round": 0, "time_in_match_in_ms": 67543, "time_in_round_in_ms": 12486},
        {"round": 0, "time_in_match_in_ms": 69685, "time_in_round_in_ms": 14628},
        {"round": 1, "time_in_match_in_ms": 150000, "time_in_round_in_ms": 9700},
    ]
    starts = calibrate.round_starts(kills)
    assert starts[0] == pytest.approx(55.057)
    assert starts[1] == pytest.approx(140.3)


def test_round_starts_accepts_models_as_well_as_dicts():
    class K:
        def __init__(self, r, m, i):
            self.round, self.time_in_match_in_ms, self.time_in_round_in_ms = r, m, i

    assert calibrate.round_starts([K(3, 310_000, 5_000)])[3] == pytest.approx(305.0)


def test_a_kill_missing_a_clock_is_skipped_not_guessed():
    kills = [{"round": 2, "time_in_match_in_ms": 1000, "time_in_round_in_ms": None}]
    assert calibrate.round_starts(kills) == {}


# ------------------------------------------------------------ reading the timer

def test_reads_the_countdown_at_the_top_centre():
    assert calibrate.read_timer(frame([timer_box("1:33")])) == 93.0
    assert calibrate.read_timer(frame([timer_box("0:06")])) == 6.0


def test_ignores_a_clock_that_is_not_where_the_round_timer_is():
    """A stream overlay keeps time too.

    A Spotify widget or a countdown in the corner reads as MM:SS just as
    well, and one of those taken for the round timer would date the frame
    confidently and wrongly - which is worse than not dating it at all.
    """
    assert calibrate.read_timer(frame([timer_box("3:21", cx=0.9)])) is None
    assert calibrate.read_timer(frame([timer_box("3:21", top=0.5)])) is None


def test_ignores_a_duration_too_long_to_be_a_countdown():
    assert calibrate.read_timer(frame([timer_box("41:07")])) is None


def test_no_timer_without_frame_dimensions():
    assert calibrate.read_timer(FrameRead((timer_box("1:00"),), 0, 0)) is None


# ------------------------------------------------------------ the vote

def test_a_buy_phase_frame_counts_down_to_the_round_starting():
    # 135s into the broadcast past the naive start, 21s left of the buy phase
    # before round 0 begins at 55.1s: the match is 101s further in than its
    # own timestamp claims.
    votes = calibrate.votes_from(135.0, 0.0, 21.0, True, STARTS)
    assert pytest.approx(TRUE_SHIFT, abs=0.1) in votes


def test_an_in_round_frame_counts_up_from_the_round_starting():
    # 165s in, timer 1:31, so 9s into round 0.
    votes = calibrate.votes_from(165.0, 0.0, 91.0, False, STARTS)
    assert pytest.approx(TRUE_SHIFT, abs=0.1) in votes


def test_a_buy_phase_offers_only_the_buy_phase_reading():
    """Said outright on screen, so the ambiguity is gone and so are its votes.

    A bare 0:20 is equally a buy phase twenty seconds from ending and a
    round eighty seconds old, and both have to be offered. "BUY PHASE" on
    the screen settles it, and half the candidates go away.
    """
    buy = calibrate.votes_from(300.0, 0.0, 20.0, True, STARTS)
    either = calibrate.votes_from(300.0, 0.0, 20.0, False, STARTS)
    assert len(buy) < len(either)
    assert set(buy) <= set(either)


def test_implausible_shifts_are_never_offered():
    votes = calibrate.votes_from(5000.0, 0.0, 30.0, False, STARTS)
    assert all(calibrate.SHIFT_MIN_S <= v <= calibrate.SHIFT_MAX_S for v in votes)


def test_the_true_shift_wins_because_rounds_are_irregular():
    """Several frames, each from a different round, agreeing on one answer.

    A shift that is out by a whole round would have to fit every one of
    them, and real rounds do not run to a fixed length - 85s, then 74s, then
    90s - so it cannot.
    """
    votes = []
    for offset, timer, is_buy in [
        (135.0, 21.0, True),    # buy phase before round 0
        (165.0, 91.0, False),   # 9s into round 0
        (195.0, 61.0, False),   # 39s into round 0
        (225.0, 17.0, True),    # buy phase before round 1
        (320.0, 95.0, False),   # 5s into round 2
    ]:
        votes += calibrate.votes_from(offset, 0.0, timer, is_buy, STARTS)
    shift, agreed = calibrate.best_cluster(votes)
    assert shift == pytest.approx(TRUE_SHIFT, abs=0.5)
    assert agreed == 5


def test_best_cluster_of_nothing_is_nothing():
    assert calibrate.best_cluster([]) == (0.0, 0)


# ------------------------------------------------------------ scanning

def test_scan_starts_after_the_naive_match_start():
    """Every plausible shift is at least 45s, so there is nothing to see before."""
    offsets = calibrate.scan_offsets(1000, (0, 9999), limit=4, step=15)
    assert offsets == [1060, 1075, 1090, 1105]


def test_scan_stays_inside_what_the_broadcast_covers():
    """A stream that joined late is read from where it joined, not before."""
    offsets = calibrate.scan_offsets(1000, (1200, 1240), limit=10, step=15)
    assert offsets == [1200, 1215, 1230]


def test_nothing_to_scan_is_not_an_error():
    assert calibrate.scan_offsets(1000, (10, 20), limit=5) == []


# ------------------------------------------------------------ measuring

class _Reader:
    available = True

    def __init__(self, by_offset):
        self.by_offset = by_offset

    def read(self, path):
        return self.by_offset.get(int(path))


class _Frames:
    def __init__(self, fail=()):
        self.fail = set(fail)

    def frame(self, vod_id, offset):
        return None if int(offset) in self.fail else str(int(offset))


def _screen(timer, buy=False):
    boxes = [timer_box(timer)]
    if buy:
        boxes.append(TextBox("BUY PHASE", 500, 100, 780, 140))
    return frame(boxes)


# A broadcast whose match begins 100s after its own timestamp. The first
# three sampling points land in agent select and show nothing; the rest walk
# through the opening buy phase and into round 0.
MEASURED = {
    1105: _screen("0:50", buy=True),
    1120: _screen("0:35", buy=True),
    1135: _screen("0:20", buy=True),
    1150: _screen("0:05", buy=True),
    1165: _screen("1:30"),
}


def test_measure_recovers_the_shift_from_a_handful_of_frames():
    cal = calibrate.measure(
        _Reader(MEASURED), _Frames(), "v1",
        base=1000, starts=STARTS, window=(0, 9999), limit=8, workers=1,
    )
    assert cal.measured
    assert cal.shift_s == pytest.approx(99.9, abs=0.5)
    assert cal.votes >= calibrate.MIN_VOTES


def test_too_few_agreeing_frames_is_an_estimate_not_a_guess():
    """Below the threshold it says so, rather than quietly using one reading."""
    cal = calibrate.measure(
        _Reader({1105: _screen("0:50", buy=True)}), _Frames(), "v1",
        base=1000, starts=STARTS, window=(0, 9999), limit=8, workers=1,
    )
    assert not cal.measured and cal.shift_s == calibrate.FALLBACK_SHIFT_S


def test_unreadable_frames_do_not_sink_the_measurement():
    """Failing to read a frame is the commonest outcome in the whole program."""
    cal = calibrate.measure(
        _Reader(MEASURED), _Frames(fail=[1120, 1150]), "v1",
        base=1000, starts=STARTS, window=(0, 9999), limit=8, workers=1,
    )
    assert cal.measured and cal.shift_s == pytest.approx(99.9, abs=0.5)


def test_without_ocr_there_is_no_measurement():
    class Blind:
        available = False

        def read(self, path):  # pragma: no cover - never reached
            raise AssertionError("should not be asked to read")

    cal = calibrate.measure(Blind(), _Frames(), "v1", base=0, starts=STARTS,
                            window=(0, 999))
    assert cal is calibrate.ESTIMATE


def test_a_match_with_no_kills_cannot_be_dated():
    cal = calibrate.measure(_Reader({}), _Frames(), "v1", base=0, starts={},
                            window=(0, 999))
    assert not cal.measured


# ------------------------------------------------------------ the offset itself

def test_moment_offset_adds_the_shift_and_subtracts_the_lead():
    assert calibrate.moment_offset(
        match_start_epoch=1000, vod_created_epoch=400,
        time_in_match_ms=120_000, shift_s=100.0, lead_s=8,
    ) == 600 + 100 + 120 - 8


def test_moment_offset_defaults_to_the_median_gap():
    naive = calibrate.moment_offset(
        match_start_epoch=1000, vod_created_epoch=400, time_in_match_ms=0,
    )
    assert naive == 600 + int(calibrate.FALLBACK_SHIFT_S)


def test_stamp_is_what_twitch_wants():
    assert calibrate.stamp(3661) == "01h01m01s"
    assert calibrate.stamp(-5) == "00h00m00s"


# ------------------------------------------------------------ storage

@pytest.fixture()
def conn(tmp_path):
    with db.session(tmp_path / "t.db") as c:
        c.execute("INSERT INTO match (id, started_at, started_at_epoch, "
                  "ingested_at, raw_json) VALUES ('m1','',0,'','{}')")
        yield c


def test_an_unmeasured_pair_reads_back_as_an_estimate(conn):
    assert calibrate.load(conn, "m1", "v1") is calibrate.ESTIMATE


def test_a_measurement_survives_a_round_trip(conn):
    calibrate.store(conn, "m1", "v1", calibrate.Calibration(101.0, "measured", 7))
    back = calibrate.load(conn, "m1", "v1")
    assert back.shift_s == 101.0 and back.measured and back.votes == 7


def test_one_broadcast_holds_several_matches(conn):
    """The reason this is not keyed on the VOD alone.

    A streamer plays several games in a sitting, and each one has its own
    agent select and its own loading screen in front of it.
    """
    conn.execute("INSERT INTO match (id, started_at, started_at_epoch, "
                 "ingested_at, raw_json) VALUES ('m2','',0,'','{}')")
    calibrate.store(conn, "m1", "v1", calibrate.Calibration(125.0, "measured", 5))
    calibrate.store(conn, "m2", "v1", calibrate.Calibration(79.0, "measured", 8))
    assert calibrate.load(conn, "m1", "v1").shift_s == 125.0
    assert calibrate.load(conn, "m2", "v1").shift_s == 79.0


def test_remeasuring_replaces_the_old_answer(conn):
    calibrate.store(conn, "m1", "v1", calibrate.ESTIMATE)
    calibrate.store(conn, "m1", "v1", calibrate.Calibration(101.0, "measured", 6))
    assert calibrate.load(conn, "m1", "v1").measured
