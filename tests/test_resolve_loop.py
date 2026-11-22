"""The shape of one match's resolve pass.

Every lookup here is a round trip the process spends idle, and there is one
name search per player plus one broadcast listing per candidate. Doing them
one at a time is what made a scan of a fortnight take as long as it did, so
they are issued together - and that restructuring is exactly the kind that
can quietly drop a player, double-count a channel, or turn "we could not
look" into "there was nothing there". These pin all three down.
"""

import threading
import time

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
        self.looked: list[tuple[str, list[float]]] = []

    def prefetch_playlists(self, vod_ids, **kw):
        ids = list(vod_ids)
        self.prefetched.append(ids)
        self.order.append("prefetch")

    def playlist(self, vod_id):
        self.singles.append(vod_id)
        return "http://example.invalid/x.m3u8"

    def iter_frames(self, vod_id, offsets, **kw):
        self.order.append(f"look:{vod_id}")
        offsets = list(offsets)
        self.looked.append((vod_id, offsets))
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


# ------------------------------------------- where in the broadcast we look

def test_frames_are_sampled_where_the_match_is_not_where_its_timestamp_says(
    resolved, sample_match
):
    """`metadata.started_at` is the queue pop, not the first round.

    The map finishes loading about a hundred seconds later - 101, 109 and 125
    on the three broadcasts that were measured - so sampling at
    `started_at + time_in_match` aims every frame a buy phase and most of a
    round early. The round-end scoreboard is the one frame that returns all
    ten names at once, and it was being missed by more than a round, which is
    why confirming the same streamer twice could come out differently.

    The same ten frames, pointed at the right seconds: no extra call, no extra
    second, and `calibrate.FALLBACK_SHIFT_S` is the median of the three.
    """
    from clipsvalorant import calibrate

    others = [p for p in sample_match.players if p.puuid != resolved.me.puuid]
    target = others[0]
    cid = f"id-{target.name.lower()}"
    start = int(sample_match.metadata.started_at.timestamp())
    vod = Vod(id="v1", channel_id=cid, stream_id=None, title="valorant ranked",
              created_at="", created_epoch=start - 600, duration_s=7200, url="")
    watcher = _Watcher()
    resolved(FakeTwitch(videos_for={cid: [vod]}), watcher=watcher)

    looked = [offs for vid, offs in watcher.frames.looked if vid == "v1"]
    assert looked, "the broadcast was never opened"
    offsets = looked[0]
    # Every sample is the naive offset plus the shift, and the first kill of
    # the fixture match is 12s in - so without the shift one of these would
    # land at 600 + 14, before the map had finished loading.
    assert min(offsets) > 600 + calibrate.FALLBACK_SHIFT_S
    naive = [o - calibrate.FALLBACK_SHIFT_S for o in offsets]
    assert all(o >= 600 for o in naive)


# ------------------------------- what an interrupted scan leaves behind

MATCH_ID = "11111111-2222-3333-4444-555555555555"

# An hour ago on the real clock, because `last_resolved_at` is written with
# the real clock too. A fixed epoch for one and `utcnow()` for the other
# makes "resolved a moment ago" read as weeks stale, which is how the first
# version of this test passed while proving nothing.
STARTED = int(time.time()) - 3600


class _StubTwitch:
    """Enough of a client for the loop to turn over. The lookups are not
    what these tests are about - where the bookkeeping lands is."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def game_id(self, name):
        return "valorant"


def _one_match(cfg, payload):
    """One real match on disk, never looked at, ready to be resolved."""
    import json

    with db.session(cfg.db_path) as conn:
        conn.execute(
            "INSERT INTO match (id, started_at, started_at_epoch, ingested_at, "
            "raw_json) VALUES (?,?,?,?,?)",
            (MATCH_ID, "", STARTED, "", json.dumps(payload["data"])),
        )
        conn.commit()


def _bookkeeping(cfg):
    with db.session(cfg.db_path) as conn:
        row = conn.execute(
            "SELECT last_resolved_at, resolve_attempts FROM match"
        ).fetchone()
    return row["last_resolved_at"], row["resolve_attempts"]


@pytest.fixture()
def loop(cfg, sample_payload, monkeypatch):
    """A database with one match in it and no network underneath."""
    from clipsvalorant import resolve as resolve_mod

    _one_match(cfg, sample_payload)
    monkeypatch.setattr(resolve_mod, "TwitchClient",
                        lambda *a, **k: _StubTwitch())
    return cfg


def test_the_attempt_is_recorded_before_the_work_not_after(loop, monkeypatch):
    """The failure this prevents is a scan that resumes perfectly and never
    advances. A host that stops part-way through a match used to leave no
    trace the match had been tried at all, so the next run chose the same
    one, died in the same place, and went on doing that forever."""
    from clipsvalorant import resolve as resolve_mod

    during: list[int] = []

    def peek(conn, *a, **k):
        during.append(
            conn.execute("SELECT resolve_attempts AS n FROM match").fetchone()["n"]
        )

    monkeypatch.setattr(resolve_mod, "resolve_match", peek)
    resolve_mod.resolve_recent(loop, limit=5, watch_vods=False)

    assert during == [1], "the attempt was not on disk while the work was running"


def test_an_interrupted_match_keeps_its_attempt_but_no_resolved_stamp(
    loop, monkeypatch
):
    """`resolve_attempts` answers "how often did we try" and
    `last_resolved_at` answers "did we get to the end". A match killed
    part-way has to say yes to the first and no to the second, or the
    due-list cannot tell a match nobody has looked at from one that defeats
    every run it meets."""
    from clipsvalorant import resolve as resolve_mod

    def die(*a, **k):
        raise RuntimeError("the host stopped")

    monkeypatch.setattr(resolve_mod, "resolve_match", die)
    with pytest.raises(RuntimeError):
        resolve_mod.resolve_recent(loop, limit=5, watch_vods=False)

    stamped, attempts = _bookkeeping(loop)
    assert attempts == 1, "an interrupted match left no record of the attempt"
    assert stamped is None, "a match that never finished was stamped resolved"


def test_a_match_that_defeats_every_run_is_eventually_left_alone(loop, monkeypatch):
    """MAX_ATTEMPTS has to be a ceiling on being interrupted and not only on
    failing. Without that, one match too slow to finish inside the window
    blocks everything behind it: every resumed scan picks it first, dies in
    the same place, and no later match is ever reached."""
    from clipsvalorant import resolve as resolve_mod
    from clipsvalorant.sweep import MAX_ATTEMPTS, matches_due

    monkeypatch.setattr(resolve_mod, "resolve_match",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cut off")))

    for _ in range(MAX_ATTEMPTS):
        with pytest.raises(RuntimeError):
            resolve_mod.resolve_recent(loop, limit=5, watch_vods=False)

    with db.session(loop.db_path) as conn:
        due, _, _ = matches_due(conn, need_highlight=False)
    assert due == [], "a match that can never finish is still being retried"


def test_a_match_that_finishes_is_stamped_and_then_skipped(loop, monkeypatch):
    """The other half of the same contract, and the whole of picking up
    where a scan left off: a match that got to the end is not asked again,
    which is what makes a resumed scan shorter than the one it follows."""
    from clipsvalorant import resolve as resolve_mod
    from clipsvalorant.sweep import matches_due

    monkeypatch.setattr(resolve_mod, "resolve_match", lambda *a, **k: None)
    resolve_mod.resolve_recent(loop, limit=5, watch_vods=False)

    stamped, attempts = _bookkeeping(loop)
    assert stamped is not None, "a finished match was not stamped as resolved"
    assert attempts == 1

    with db.session(loop.db_path) as conn:
        due, _, settled = matches_due(conn, need_highlight=False)
    assert due == [], "a match resolved a moment ago was offered up again"
    assert settled == 1
