"""Pulling frames out of a broadcast.

The only thing here that is not a thin wrapper around ffmpeg is the prefetch
iterator, and it carries two promises the callers depend on: frames arrive in
the order they were asked for, and frames past the point the caller stops are
never fetched at all. Both matter because every caller stops early - `watch`
the moment the answer is certain - and a frame costs about two seconds.
"""

import threading
import time

from clipsvalorant.vod import FrameReader


class _Reader(FrameReader):
    """A FrameReader whose frames cost time but no network."""

    def __init__(self, cache_dir, *, delay=0.0, missing=()):
        super().__init__(cache_dir=cache_dir)
        self.delay = delay
        self.missing = set(missing)
        self.fetched: list[float] = []
        self._guard = threading.Lock()

    def playlist(self, vod_id):
        return "http://example.invalid/playlist.m3u8"

    def frame(self, vod_id, offset_s):
        if self.delay:
            time.sleep(self.delay)
        with self._guard:
            self.fetched.append(offset_s)
        return None if offset_s in self.missing else f"frame-{offset_s}"


def test_frames_arrive_in_the_order_they_were_asked_for(tmp_path):
    """A vote is only meaningful against the offset it was read at, and round
    ends are tried before killfeed frames because they are worth more. Both
    break if prefetching is allowed to reorder."""
    r = _Reader(tmp_path)
    offsets = [50.0, 10.0, 90.0, 30.0, 70.0]
    got = [off for off, _ in r.iter_frames("v1", offsets)]
    assert got == offsets


def test_stopping_early_does_not_fetch_the_rest(tmp_path):
    """The whole point: on a channel that is obviously the right one, most of
    the budget is never spent."""
    r = _Reader(tmp_path)
    offsets = [float(i) for i in range(40)]
    for i, (_off, _path) in enumerate(r.iter_frames("v1", offsets, ahead=3)):
        if i == 1:
            break
    time.sleep(0.05)  # let any in-flight worker finish
    assert len(r.fetched) <= 5     # the two yielded, plus what was in flight
    assert len(r.fetched) < len(offsets)


def test_a_frame_that_will_not_read_is_reported_not_raised(tmp_path):
    """Failing to read a frame is the commonest outcome in this program."""
    r = _Reader(tmp_path, missing={2.0})
    out = dict(r.iter_frames("v1", [1.0, 2.0, 3.0]))
    assert out[1.0] and out[3.0]
    assert out[2.0] is None


def test_a_worker_that_raises_does_not_take_down_the_pass(tmp_path):
    class Boom(_Reader):
        def frame(self, vod_id, offset_s):
            if offset_s == 2.0:
                raise RuntimeError("ffmpeg fell over")
            return super().frame(vod_id, offset_s)

    r = Boom(tmp_path)
    out = dict(r.iter_frames("v1", [1.0, 2.0, 3.0]))
    assert out[2.0] is None
    assert out[1.0] and out[3.0]


def test_fetching_overlaps_rather_than_queueing(tmp_path):
    """Four frames at a tenth of a second each take a tenth of a second, not
    four. This is the whole saving."""
    r = _Reader(tmp_path, delay=0.1)
    offsets = [1.0, 2.0, 3.0, 4.0]
    t0 = time.perf_counter()
    list(r.iter_frames("v1", offsets, ahead=4))
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.3   # serial would be 0.4s plus overhead


def test_no_offsets_is_not_an_error(tmp_path):
    assert list(FrameReader(cache_dir=tmp_path).iter_frames("v1", [])) == []
