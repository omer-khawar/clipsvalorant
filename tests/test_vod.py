"""Pulling frames out of a broadcast.

The only thing here that is not a thin wrapper around ffmpeg is the prefetch
iterator, and it carries two promises the callers depend on: frames arrive in
the order they were asked for, and frames past the point the caller stops are
never fetched at all. Both matter because every caller stops early - `watch`
the moment the answer is certain - and a frame costs about two seconds.
"""

import threading
import time

from clipsvalorant.vod import FrameReader, one_segment_playlist, parse_index


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


# --------------------------------------------------------- playlist prefetch

class _Resolver(FrameReader):
    """A FrameReader whose playlist lookups cost time but no network."""

    def __init__(self, cache_dir, *, delay=0.0, dead=()):
        super().__init__(cache_dir=cache_dir)
        self.delay = delay
        self.dead = set(dead)
        self.resolved: list[str] = []
        self._guard = threading.Lock()
        self.live = 0
        self.peak = 0

    def _resolve(self, vod_id):
        with self._guard:
            self.resolved.append(vod_id)
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            if self.delay:
                time.sleep(self.delay)
            return None if vod_id in self.dead else f"http://example.invalid/{vod_id}.m3u8"
        finally:
            with self._guard:
                self.live -= 1


def test_playlists_are_resolved_together(tmp_path):
    """The whole point. Resolving one costs about two and a half seconds and
    is almost all waiting, so a lobby with four broadcasts used to spend ten
    seconds doing nothing."""
    r = _Resolver(tmp_path, delay=0.15)
    ids = ["a", "b", "c", "d"]
    started = time.perf_counter()
    r.prefetch_playlists(ids)
    took = time.perf_counter() - started

    assert sorted(r.resolved) == ids
    assert r.peak > 1, "the lookups did not overlap: they are still serial"
    assert took < 0.15 * len(ids), f"took {took:.2f}s, barely better than serial"


def test_a_prefetched_playlist_is_not_looked_up_again(tmp_path):
    """`playlist` must answer from the cache afterwards, or the prefetch has
    bought nothing and every lookup is simply done twice."""
    r = _Resolver(tmp_path)
    r.prefetch_playlists(["a", "b"])
    assert r.resolved == ["a", "b"] or sorted(r.resolved) == ["a", "b"]
    before = len(r.resolved)
    assert r.playlist("a") == "http://example.invalid/a.m3u8"
    assert r.playlist("b") == "http://example.invalid/b.m3u8"
    assert len(r.resolved) == before, "a prefetched playlist was resolved again"


def test_one_already_known_playlist_is_left_alone(tmp_path):
    r = _Resolver(tmp_path)
    r.playlist("a")
    r.prefetch_playlists(["a", "a", "b"])
    assert r.resolved.count("a") == 1, "a cached playlist was resolved twice"
    assert "b" in r.resolved


def test_a_dead_vod_is_remembered_as_dead_not_retried(tmp_path):
    """A VOD past Twitch's retention has no playlist, and that is the single
    commonest outcome in a sweep. Treating it as a transient miss would mean
    re-asking yt-dlp about it on every frame."""
    r = _Resolver(tmp_path, dead={"gone"})
    r.prefetch_playlists(["gone", "here"])
    before = len(r.resolved)
    assert r.playlist("gone") is None
    assert len(r.resolved) == before, "a dead VOD was looked up a second time"


def test_one_lookup_that_explodes_does_not_cost_the_others(tmp_path):
    """A pool that lets an exception escape loses the whole lobby's frames."""
    class Angry(_Resolver):
        def _resolve(self, vod_id):
            if vod_id == "bad":
                raise RuntimeError("yt-dlp fell over")
            return super()._resolve(vod_id)

    r = Angry(tmp_path)
    r.prefetch_playlists(["bad", "good"])
    assert r.playlist("bad") is None
    assert r.playlist("good") == "http://example.invalid/good.m3u8"


def test_nothing_to_prefetch_is_not_an_error(tmp_path):
    r = _Resolver(tmp_path)
    r.prefetch_playlists([])
    r.prefetch_playlists(["", None])
    assert r.resolved == []


# ---- finding the segment ourselves ---------------------------------------
#
# Handing ffmpeg the whole playlist and an -ss an hour deep worked on one
# machine and, on the deployed one, downloaded the broadcast from the start
# until the timeout killed it - so every frame failed and Layer 5 confirmed
# nothing while looking exactly like a broadcast with nothing in it. These
# cover the arithmetic that replaced it.

BASE = "https://cdn.invalid/abc_nika_123/720p60/index-dvr.m3u8"

PLAYLIST = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:10
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-MAP:URI="init-0.mp4"
#EXTINF:10.000,
0.mp4
#EXTINF:10.000,
1.mp4
#EXTINF:4.500,
2.mp4
#EXTINF:10.000,
3.mp4
#EXT-X-ENDLIST
"""


def test_segment_urls_resolve_against_the_playlist():
    """Twitch names segments `1017.mp4` and nothing else. Resolved against the
    wrong base they are 404s, which would look like a VOD that had expired."""
    index = parse_index(PLAYLIST, BASE)
    assert index is not None
    assert index.urls[0] == "https://cdn.invalid/abc_nika_123/720p60/0.mp4"
    assert index.init == "https://cdn.invalid/abc_nika_123/720p60/init-0.mp4"


def test_a_segment_is_found_by_adding_up_the_durations():
    """Not by dividing by the target duration. Segments are usually ten
    seconds and are not promised to be: one short segment early shifts every
    later one, and a frame read from the wrong moment is worse than none."""
    index = parse_index(PLAYLIST, BASE)
    assert index.covering(0) == (0, 0.0)
    assert index.covering(15) == (1, 5.0)
    # 2.mp4 runs 20.0 -> 24.5, so 24.0 is four seconds into it ...
    assert index.covering(24) == (2, 4.0)
    # ... and 24.5 is already the next one, which a /10 would have missed.
    assert index.covering(24.5) == (3, 0.0)
    assert index.total == 34.5


def test_a_moment_the_broadcast_does_not_reach_is_not_a_failure():
    """A stream that joined late or stopped early does not hold the moment we
    wanted. Saying so costs nothing; it used to cost a walk through the VOD."""
    index = parse_index(PLAYLIST, BASE)
    assert index.covering(34.5) is None
    assert index.covering(9_000) is None
    assert index.covering(-1) is None


def test_a_master_playlist_is_not_a_segment_list():
    """The URIs in one are renditions, not segments. Read as segments they
    would be four frames of nothing; better to report no index and fall back."""
    master = (
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080\n'
        "chunked/index-dvr.m3u8\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720\n'
        "720p60/index-dvr.m3u8\n"
    )
    assert parse_index(master, BASE) is None
    assert parse_index("", BASE) is None


def test_the_one_segment_playlist_carries_the_init_and_one_segment():
    """Fragmented MP4 will not decode without the `#EXT-X-MAP` fragment, and a
    second segment in here would be a second download for no extra evidence."""
    index = parse_index(PLAYLIST, BASE)
    text = one_segment_playlist(index, 2)
    assert text.count("#EXTINF:") == 1
    assert "https://cdn.invalid/abc_nika_123/720p60/2.mp4" in text
    assert '#EXT-X-MAP:URI="https://cdn.invalid/abc_nika_123/720p60/init-0.mp4"' in text
    assert "#EXT-X-ENDLIST" in text
    # VOD, not EVENT: it is complete, and nothing should wait for more.
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in text


def test_an_mpeg_ts_vod_needs_no_init_segment():
    """Older VODs have no `#EXT-X-MAP`. Inventing one would break them."""
    plain = "#EXTM3U\n#EXTINF:10.000,\n0.ts\n#EXT-X-ENDLIST\n"
    index = parse_index(plain, BASE)
    assert index.init is None
    assert "#EXT-X-MAP" not in one_segment_playlist(index, 0)


class _Spy(FrameReader):
    """A FrameReader that records the ffmpeg arguments instead of running it."""

    def __init__(self, cache_dir, *, index_text=PLAYLIST):
        super().__init__(cache_dir=cache_dir)
        self.index_text = index_text
        self.calls: list[list[str]] = []

    def playlist(self, vod_id):
        return BASE

    def _fetch_index(self, url):
        return parse_index(self.index_text, url) if self.index_text else None

    def _run_ffmpeg(self, args, dest):
        self.calls.append(list(args))
        dest.write_bytes(b"x" * 2048)
        return dest


def test_a_deep_frame_never_hands_ffmpeg_the_whole_playlist(tmp_path):
    """The bug, in one assertion. `-i <the whole index>` with a large `-ss` is
    what downloaded gigabytes and timed out; what goes in now is a local
    playlist holding the one segment, and a seek of a few seconds inside it."""
    r = _Spy(tmp_path)
    assert r.frame("v1", 24) is not None
    args = r.calls[0]
    assert BASE not in args
    assert args[args.index("-i") + 1].endswith(".m3u8")
    assert args[args.index("-ss") + 1] == "4.000"
    # The temporary playlist is cleaned up behind it.
    assert list(tmp_path.glob("*.m3u8")) == []


def test_a_frame_past_the_end_costs_no_ffmpeg_at_all(tmp_path):
    r = _Spy(tmp_path)
    assert r.frame("v1", 9_000) is None
    assert r.calls == []


def test_an_unreadable_playlist_still_gets_one_attempt(tmp_path):
    """Falling back matters: on a build that seeks properly the old path is
    fine, and refusing to try would turn a parse we got wrong into no frames."""
    r = _Spy(tmp_path, index_text="")
    assert r.frame("v1", 24) is not None
    args = r.calls[0]
    assert args[args.index("-i") + 1] == BASE
    assert args[args.index("-ss") + 1] == "24"


def test_the_index_is_fetched_once_per_vod(tmp_path):
    """Four threads pull frames out of one VOD. Fetching the playlist four
    times would undo most of what this saves."""
    r = _Spy(tmp_path)
    fetched = []
    original = r._fetch_index

    def counting(url):
        fetched.append(url)
        return original(url)

    r._fetch_index = counting
    for offset in (1, 12, 24, 31):
        r.frame("v1", offset)
    assert len(fetched) == 1
