"""End-to-end persistence, without touching the network."""

from clipsvalorant import db
from clipsvalorant.ingest import ingest_file, persist_match

FIXTURE = "tests/fixtures/match_v4_sample.json"


def test_ingest_file_stores_everything(cfg):
    report = ingest_file(cfg, FIXTURE)
    assert report.stored == 1
    assert report.highlights == 9

    with db.session(cfg.db_path) as conn:
        match = conn.execute("SELECT * FROM match").fetchone()
        assert match["map"] == "Ascent"
        assert match["mode"] is None or match["queue"] == "Competitive"
        assert match["my_team"] == "Blue"
        assert match["my_team_won"] == 1
        assert match["rounds_won"] == 13
        assert match["game_length_ms"] == 1_920_000

        # started_at 2026-11-15T20:04:11Z, plus a 1,920,000ms game
        assert match["ended_at_epoch"] == match["started_at_epoch"] + 1920

        assert conn.execute("SELECT COUNT(*) c FROM match_player").fetchone()["c"] == 10
        assert conn.execute("SELECT COUNT(*) c FROM kill").fetchone()["c"] == 68
        assert conn.execute("SELECT COUNT(*) c FROM highlight").fetchone()["c"] == 9

        mine = conn.execute("SELECT COUNT(*) c FROM kill WHERE is_mine").fetchone()["c"]
        # rounds 0/2/4/6/8/10/12/14 contribute 1+5+3+2+2+6+4+5,
        # then the revive rounds 16/18/20 add 1+2+2
        assert mine == 33

        me = conn.execute("SELECT * FROM match_player WHERE is_me").fetchone()
        assert me["name"] == "TestMe"
        assert me["agent"] == "Jett"

        acct = conn.execute("SELECT * FROM riot_account WHERE is_me").fetchone()
        assert acct["name"] == "TestMe"
        assert conn.execute("SELECT COUNT(*) c FROM riot_account").fetchone()["c"] == 10


def test_ingest_is_idempotent(cfg):
    ingest_file(cfg, FIXTURE)
    ingest_file(cfg, FIXTURE)

    with db.session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM match").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM kill").fetchone()["c"] == 68
        assert conn.execute("SELECT COUNT(*) c FROM highlight").fetchone()["c"] == 9
        assert conn.execute("SELECT COUNT(*) c FROM match_player").fetchone()["c"] == 10


def test_wrong_riot_id_fails_loudly(cfg, sample_match):
    """The most likely first-run mistake is a typo'd RIOT_NAME, so say so clearly."""
    import pytest

    with db.session(cfg.db_path) as conn:
        with pytest.raises(LookupError) as excinfo:
            persist_match(
                conn, sample_match, my_name="Typo", my_tag="9999", region="eu"
            )
    message = str(excinfo.value)
    assert "Typo#9999" in message
    assert "TestMe#0001" in message  # the real roster is shown


def test_raw_json_is_preserved(cfg):
    """Schemas drift; the raw payload is the source of truth."""
    import json

    ingest_file(cfg, FIXTURE)
    with db.session(cfg.db_path) as conn:
        raw = conn.execute("SELECT raw_json FROM match").fetchone()["raw_json"]
    restored = json.loads(raw)
    assert restored["metadata"]["map"]["name"] == "Ascent"
    assert len(restored["kills"]) == 68


def test_known_match_ids(cfg):
    with db.session(cfg.db_path) as conn:
        assert db.known_match_ids(conn) == set()
    ingest_file(cfg, FIXTURE)
    with db.session(cfg.db_path) as conn:
        assert db.known_match_ids(conn) == {"11111111-2222-3333-4444-555555555555"}


# ------------------------------------------------- the window, before fetching

class _Entry:
    """A row of the stored-matches listing: an id and a date, nothing more."""

    def __init__(self, match_id, started_at):
        self.meta = type("M", (), {"id": match_id, "started_at": started_at})()


class _Client:
    """A HenrikDev client that pages the way the real listing does.

    Newest first, `size` at a time, offset by `page` - so a walk that varies
    the size between calls gets overlapping pages, which is what the real
    endpoint does and what the fake has to do if the test is to mean
    anything. `honours_page=False` is the other failure worth modelling: a
    parameter that is accepted and ignored.
    """

    def __init__(self, listed, *, honours_page=True):
        self.listed = listed
        self.honours_page = honours_page
        self.calls = []
        self.detail_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stored_matches(self, name, tag, region, *, mode=None, size=20, page=1):
        self.calls.append((size, page))
        if not self.honours_page:
            page = 1
        start = (page - 1) * size
        return self.listed[start:start + size]

    def match_detail(self, match_id, region):
        self.detail_calls.append(match_id)
        raise LookupError("detail not needed for this test")


def _listing(now, ages_days):
    from datetime import timedelta
    return [_Entry(f"m{i}", now - timedelta(days=d))
            for i, d in enumerate(ages_days)]


def test_a_match_past_the_window_is_never_fetched(cfg, monkeypatch):
    """The listing already carries the date, and the detail call is the
    expensive half - HenrikDev charges per call plus whatever Riot requests it
    triggers, and sleeps the window out when it runs dry. Fetching forty
    matches to resolve twelve was most of the wall clock."""
    from datetime import datetime, timezone
    from clipsvalorant.ingest import ingest

    now = datetime.now(timezone.utc)
    client = _Client(_listing(now, [1, 3, 20, 40, 2]))
    monkeypatch.setattr("clipsvalorant.ingest.HenrikClient", lambda *a, **k: client)

    cutoff = int(now.timestamp()) - 14 * 86400
    report = ingest(cfg, limit=20, since_epoch=cutoff)

    assert report.too_old == 2                      # the 20 and 40 day old ones
    assert sorted(client.detail_calls) == ["m0", "m1", "m4"]
    assert "past the window" in report.summary()


def test_no_window_means_fetch_everything_listed(cfg, monkeypatch):
    from datetime import datetime, timezone
    from clipsvalorant.ingest import ingest

    now = datetime.now(timezone.utc)
    client = _Client(_listing(now, [1, 40]))
    monkeypatch.setattr("clipsvalorant.ingest.HenrikClient", lambda *a, **k: client)

    report = ingest(cfg, limit=20)
    assert report.too_old == 0
    assert len(client.detail_calls) == 2


def test_a_listing_with_no_date_is_fetched_rather_than_assumed(cfg, monkeypatch):
    """A field that moved or vanished must not silently empty a whole scan."""
    from datetime import datetime, timezone
    from clipsvalorant.ingest import ingest

    now = datetime.now(timezone.utc)
    entry = _Entry("m0", None)
    client = _Client([entry])
    monkeypatch.setattr("clipsvalorant.ingest.HenrikClient", lambda *a, **k: client)

    report = ingest(cfg, limit=5, since_epoch=int(now.timestamp()))
    assert report.too_old == 0
    assert client.detail_calls == ["m0"]


# ------------------------------------------------- walking the listing's pages

def _walk(client, *, limit, since_epoch=None):
    """Run the listing walk on its own, and hand back what it kept."""
    from types import SimpleNamespace

    from clipsvalorant import ingest as mod
    from clipsvalorant.cancel import NullToken

    report = mod.IngestReport()
    cfg = SimpleNamespace(riot_name="TestMe", riot_tag="0001", region="eu")
    kept = mod._list_window(
        client, cfg, mode="competitive", limit=limit,
        since_epoch=since_epoch, report=report, cancel=NullToken(),
    )
    return kept, report


def _daily(count):
    """`count` matches, one a day, newest first - the shape of a real history."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return now, _listing(now, list(range(1, count + 1)))


def _cutoff(now, days):
    from datetime import timedelta
    return int((now - timedelta(days=days)).timestamp())


def test_a_window_wider_than_one_page_asks_for_the_next_one(monkeypatch):
    """The whole bug in one test: 100 is the biggest page the endpoint
    returns, so a window that holds more than a page of matches can only be
    covered by asking for page two. Choosing 30 days used to mean 'the 100
    most recent matches', which for an active player was 21 days."""
    monkeypatch.setattr("clipsvalorant.ingest.PAGE_SIZE", 10)
    now, listed = _daily(30)
    client = _Client(listed)

    kept, report = _walk(client, limit=100, since_epoch=_cutoff(now, 25))

    assert client.calls == [(10, 1), (10, 2), (10, 3)]
    assert report.pages == 3
    assert len(kept) == 30
    assert report.complete is True


def test_the_walk_stops_at_the_cutoff_not_at_the_end_of_history(monkeypatch):
    """Depth comes from the window. Pages past it are calls nobody needs."""
    monkeypatch.setattr("clipsvalorant.ingest.PAGE_SIZE", 10)
    now, listed = _daily(30)
    client = _Client(listed)

    kept, report = _walk(client, limit=100, since_epoch=_cutoff(now, 15))

    assert client.calls == [(10, 1), (10, 2)]   # page 3 is all past the window
    assert len(kept) == 20
    assert report.complete is True


def test_the_page_size_never_changes_mid_walk(monkeypatch):
    """Which matches page two holds depends on the size page one was asked
    with. Shrinking the size to fit the remaining ceiling asks for a page
    that overlaps the one already read, and that overlap is indistinguishable
    from the end of the history."""
    monkeypatch.setattr("clipsvalorant.ingest.PAGE_SIZE", 10)
    now, listed = _daily(40)
    client = _Client(listed)

    kept, report = _walk(client, limit=25, since_epoch=_cutoff(now, 35))

    assert {size for size, _ in client.calls} == {10}
    assert [e.meta.id for e in kept] == [f"m{i}" for i in range(25)]
    assert report.complete is False


def test_a_page_parameter_that_does_nothing_stops_the_walk(monkeypatch):
    """A full page of matches already read means `page` was accepted and
    ignored. That is not the end of the history, and it must not be reported
    as a window covered."""
    monkeypatch.setattr("clipsvalorant.ingest.PAGE_SIZE", 10)
    now, listed = _daily(30)
    client = _Client(listed, honours_page=False)

    kept, report = _walk(client, limit=100, since_epoch=_cutoff(now, 25))

    assert len(client.calls) == 2        # asked twice, got the same page
    assert len(kept) == 10
    assert report.complete is False


def test_a_short_page_is_the_end_of_the_history(monkeypatch):
    monkeypatch.setattr("clipsvalorant.ingest.PAGE_SIZE", 10)
    now, listed = _daily(14)
    client = _Client(listed)

    kept, report = _walk(client, limit=100, since_epoch=_cutoff(now, 30))

    assert client.calls == [(10, 1), (10, 2)]
    assert len(kept) == 14
    assert report.complete is True       # 14 matches is all there is


def test_the_ceiling_stopping_the_walk_is_not_called_complete(monkeypatch):
    monkeypatch.setattr("clipsvalorant.ingest.PAGE_SIZE", 10)
    now, listed = _daily(30)
    client = _Client(listed)

    kept, report = _walk(client, limit=12, since_epoch=_cutoff(now, 25))

    assert len(kept) == 12
    assert report.complete is False
    assert "stopped at the ceiling" in report.summary()


def test_a_page_that_crosses_the_cutoff_and_fills_the_ceiling_is_complete(monkeypatch):
    """Everything the trim threw away was already past the window, so the
    window was covered. Warning here would cry wolf on every busy scan."""
    monkeypatch.setattr("clipsvalorant.ingest.PAGE_SIZE", 10)
    now, listed = _daily(30)
    client = _Client(listed)

    kept, report = _walk(client, limit=12, since_epoch=_cutoff(now, 11))

    assert len(kept) == 12
    assert report.complete is True
    assert "stopped at the ceiling" not in report.summary()


def test_the_oldest_listed_is_what_was_kept_not_what_was_fetched(monkeypatch):
    monkeypatch.setattr("clipsvalorant.ingest.PAGE_SIZE", 10)
    now, listed = _daily(30)
    client = _Client(listed)

    kept, report = _walk(client, limit=12, since_epoch=_cutoff(now, 11))

    assert report.oldest_listed == _cutoff(now, 12)


def test_a_small_ceiling_still_costs_exactly_one_call(monkeypatch):
    """The default 40-match scan must not become four calls."""
    now, listed = _daily(60)
    client = _Client(listed)

    kept, report = _walk(client, limit=40, since_epoch=_cutoff(now, 30))

    assert client.calls == [(40, 1)]
    assert report.pages == 1
    assert len(kept) == 40


def test_ingest_walks_the_pages_and_still_drops_what_is_past_the_window(cfg, monkeypatch):
    """End to end: the walk is wired in, and the window filter it feeds
    still keeps the expensive detail call off matches that are too old."""
    from clipsvalorant.ingest import ingest

    monkeypatch.setattr("clipsvalorant.ingest.PAGE_SIZE", 10)
    now, listed = _daily(25)
    client = _Client(listed)
    monkeypatch.setattr("clipsvalorant.ingest.HenrikClient", lambda *a, **k: client)

    report = ingest(cfg, limit=100, since_epoch=_cutoff(now, 20))

    assert report.pages == 3
    assert report.seen == 25
    assert len(client.detail_calls) == 20
    assert report.too_old == 5
    assert report.complete is True
    assert "3 listing pages" in report.summary()
