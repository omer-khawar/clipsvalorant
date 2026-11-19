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
    """A HenrikDev client that counts what it was actually asked to fetch."""

    def __init__(self, listed):
        self.listed = listed
        self.detail_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stored_matches(self, name, tag, region, *, mode=None, size=20):
        return self.listed[:size]

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
