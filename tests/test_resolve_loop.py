"""The shape of one match's resolve pass.

Every lookup here is a round trip the process spends idle, and there is one
name search per player plus one broadcast listing per candidate. Doing them
one at a time is what made a scan of a fortnight take as long as it did, so
they are issued together - and that restructuring is exactly the kind that
can quietly drop a player, double-count a channel, or turn "we could not
look" into "there was nothing there". These pin all three down.
"""

import threading

import pytest

from clipsvalorant import db
from clipsvalorant.resolve import resolve_match
from clipsvalorant.twitch import Vod


def _hit(login, channel_id=None):
    return {"id": channel_id or f"id-{login}", "broadcaster_login": login,
            "display_name": login, "description": ""}


class FakeTwitch:
    """Records every call, and how many ran at once."""

    def __init__(self, *, videos_for=None, search_fails=(), videos_fails=()):
        self.videos_for = videos_for or {}
        self.search_fails = set(search_fails)
        self.videos_fails = set(videos_fails)
        self.searched: list[str] = []
        self.videos_calls: list[str] = []
        self._lock = threading.Lock()
        self.peak = 0
        self._live = 0

    def _enter(self):
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)

    def _leave(self):
        with self._lock:
            self._live -= 1

    def search_channels(self, name, *, limit=20):
        self._enter()
        try:
            with self._lock:
                self.searched.append(name)
            if name in self.search_fails:
                raise RuntimeError("twitch said no")
            return [_hit(name.lower())]
        finally:
            self._leave()

    def videos(self, channel_id, *, limit=100, kind="archive"):
        self._enter()
        try:
            with self._lock:
                self.videos_calls.append(channel_id)
            if channel_id in self.videos_fails:
                raise RuntimeError("videos exploded")
            return self.videos_for.get(channel_id, [])
        finally:
            self._leave()

    def users(self, *, logins=None, ids=None):
        return []

    def live_now(self, channel_ids):
        return {}

    def channel_clips(self, channel_id, started, ended):
        return []

    def clips_in_window(self, *a, **k):
        return []


@pytest.fixture
def resolved(cfg, sample_match):
    """Run resolve_match over the fixture lobby with a fake Twitch."""
    from clipsvalorant.ingest import persist_match

    me = sample_match.players[0]
    with db.session(cfg.db_path) as conn:
        persist_match(conn, sample_match, my_name=me.name, my_tag=me.tag,
                      region=cfg.region)
        conn.commit()

    def run(twitch, **kwargs):
        from clipsvalorant.resolve import ResolveReport
        report = ResolveReport()
        kwargs.setdefault("watcher", None)
        kwargs.setdefault("game_id", None)
        with db.session(cfg.db_path) as conn:
            resolve_match(
                conn, twitch, sample_match,
                my_puuid=me.puuid, report=report, **kwargs,
            )
        return report

    run.me = me
    return run


def test_every_lobby_member_is_searched_for(resolved, sample_match):
    """Nine other players, nine searches. Issuing them together must not
    drop one, and a dropped player is a streamer never found."""
    tw = FakeTwitch()
    report = resolved(tw)
    others = [p for p in sample_match.players if p.puuid != resolved.me.puuid]
    assert report.players_checked == len(others)
    assert sorted(tw.searched) == sorted(p.name for p in others)


def test_the_searches_actually_overlap(resolved):
    """The whole point of the change. One at a time is nine round trips.

    Proved with a barrier rather than a stopwatch: if the searches were still
    issued one after another, the first would wait for partners that cannot
    arrive and the barrier would time out.
    """
    gate = threading.Barrier(3, timeout=5)
    reached = []

    class Blocking(FakeTwitch):
        def search_channels(self, name, *, limit=20):
            with self._lock:
                self.searched.append(name)
            try:
                gate.wait()
                reached.append(name)
            except threading.BrokenBarrierError:
                pytest.fail("searches did not overlap: they are still serial")
            return [_hit(name.lower())]

    resolved(Blocking())
    assert len(reached) >= 3


def test_one_players_failed_search_does_not_cost_the_others(resolved, sample_match):
    """A thread pool that lets one exception escape loses the whole lobby."""
    others = [p for p in sample_match.players if p.puuid != resolved.me.puuid]
    doomed = others[0].name
    tw = FakeTwitch(search_fails={doomed})
    report = resolved(tw)

    assert any(doomed in err and "search failed" in err for err in report.errors)
    # everyone else still got looked at
    assert len(tw.videos_calls) >= len(others) - 1


def test_a_channel_is_asked_for_its_broadcasts_once(resolved, sample_match):
    """A name search routinely returns the same big streamer for several
    players. Fetching their listing once per candidate is pure waste."""
    class Collides(FakeTwitch):
        def search_channels(self, name, *, limit=20):
            self._enter()
            try:
                with self._lock:
                    self.searched.append(name)
                # Every player's name resolves to one channel: the extreme of
                # the real case, where one big streamer answers to several
                # lobby names at once.
                return [_hit(name.lower(), "shared-id")]
            finally:
                self._leave()

    tw = Collides()
    resolved(tw)
    assert len(tw.searched) > 1, "the lobby was not searched"
    assert tw.videos_calls.count("shared-id") == 1


def test_a_failed_listing_is_reported_not_read_as_no_broadcast(resolved, sample_match):
    """"We could not look" and "there was nothing there" are the two answers
    this program most needs to tell apart - the whole verdict ladder rests on
    it. A prefetch that swallowed the error would turn every outage into a
    confident rejection."""
    others = [p for p in sample_match.players if p.puuid != resolved.me.puuid]
    broken = f"id-{others[0].name.lower()}"
    tw = FakeTwitch(videos_fails={broken})
    report = resolved(tw)
    assert any("videos failed" in err and "videos exploded" in err
               for err in report.errors)


def test_a_lobby_with_no_candidates_asks_for_no_listings(resolved):
    class Nothing(FakeTwitch):
        def search_channels(self, name, *, limit=20):
            with self._lock:
                self.searched.append(name)
            return []

    tw = Nothing()
    resolved(tw)
    assert tw.videos_calls == []


def test_a_covering_broadcast_still_reaches_the_evidence(resolved, sample_match):
    """The restructure must not change what gather_evidence sees."""
    others = [p for p in sample_match.players if p.puuid != resolved.me.puuid]
    target = others[0]
    cid = f"id-{target.name.lower()}"
    start = int(sample_match.metadata.started_at.timestamp())
    vod = Vod(id="v9", channel_id=cid, stream_id=None, title="valorant ranked",
              created_at="", created_epoch=start - 600, duration_s=7200, url="")
    tw = FakeTwitch(videos_for={cid: [vod]})
    report = resolved(tw)

    filed = [c for c in report.candidates + report.confirmed
             if c.channel_id == cid]
    assert filed, "the candidate with a covering broadcast was not filed"
    assert filed[0].evidence.vod_overlap_s > 0


# ------------------------------------------- opening the broadcasts together

class _Frames:
    """Stands in for FrameReader, recording how the playlists were asked for."""

    def __init__(self):
        self.prefetched: list[list[str]] = []
        self.singles: list[str] = []
        self.order: list[str] = []

    def prefetch_playlists(self, vod_ids, **kw):
        ids = list(vod_ids)
        self.prefetched.append(ids)
        self.order.append("prefetch")

    def playlist(self, vod_id):
        self.singles.append(vod_id)
        return "http://example.invalid/x.m3u8"

    def iter_frames(self, vod_id, offsets, **kw):
        self.order.append(f"look:{vod_id}")
        for off in offsets:
            yield off, None


class _Reader:
    available = True

    def read(self, path):
        return None


class _Watcher:
    def __init__(self):
        self.frames = _Frames()
        self.reader = _Reader()
        self.limit = 4
        self.ready = True


def _vods_for(cands):
    return sorted({c.channel_id for c in cands})


def test_every_broadcast_is_opened_before_any_of_them_is_read(resolved, sample_match):
    """Resolving one VOD's playlist is about two and a half seconds of pure
    waiting - half of it yt-dlp starting up - so doing them one at a time
    inside the loop is the longest avoidable stall in a scan."""
    others = [p for p in sample_match.players if p.puuid != resolved.me.puuid]
    start = int(sample_match.metadata.started_at.timestamp())
    vods = {}
    for i, p in enumerate(others[:3]):
        cid = f"id-{p.name.lower()}"
        vods[cid] = [Vod(id=f"v{i}", channel_id=cid, stream_id=None,
                         title="valorant ranked", created_at="",
                         created_epoch=start - 600, duration_s=7200, url="")]
    watcher = _Watcher()
    resolved(FakeTwitch(videos_for=vods), watcher=watcher)

    assert watcher.frames.prefetched, "the playlists were never opened together"
    asked = [v for batch in watcher.frames.prefetched for v in batch]
    assert sorted(asked) == sorted(f"v{i}" for i in range(3)), asked

    # and nothing was read before they were all open
    first_look = next(i for i, step in enumerate(watcher.frames.order)
                      if step.startswith("look:"))
    assert watcher.frames.order[:first_look].count("prefetch") >= 1


def test_a_lobby_with_no_broadcasts_opens_nothing(resolved):
    watcher = _Watcher()
    resolved(FakeTwitch(), watcher=watcher)
    assert all(batch == [] for batch in watcher.frames.prefetched)
    assert not any(s.startswith("look:") for s in watcher.frames.order)


def test_the_summary_counts_likely_apart_from_unverified():
    """`candidates` holds two different things, and only one of them is shown.

    Reported from a real scan: the line said five confirmed and five
    unverified, so ten were expected on a page that only ever shows
    confirmed and likely. Counting them together turns "we could not judge
    this one" into a promise of something to watch.
    """
    from types import SimpleNamespace

    from clipsvalorant.resolve import ResolveReport

    report = ResolveReport()
    for verdict in ("confirmed", "likely", "unconfirmed", "unconfirmed", "rejected"):
        report.file(SimpleNamespace(verdict=verdict))

    assert len(report.confirmed) == 1
    assert len(report.likely) == 1
    assert len(report.unverified) == 2
    assert len(report.candidates) == 3  # likely and unconfirmed, as before

    line = report.summary()
    assert "1 confirmed" in line and "1 likely" in line
    assert "2 unverified" in line
