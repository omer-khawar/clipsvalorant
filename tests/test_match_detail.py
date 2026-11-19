"""The detail the v4 payload was always sending and we were throwing away.

`raw_json` is stored as the source of truth so detection logic can change
without re-fetching. For a long while what actually reached the disk was this
project's own model dump - and the model ignores fields it has not declared,
so kill coordinates, per-player damage and the map's uuid were dropped on the
way in, for sixty-seven matches. These tests are mostly about that class of
loss: parse it, store it, and prove it survives the round trip.
"""

import json

import pytest

from clipsvalorant import db
from clipsvalorant.ingest import needs_backfill, persist_match
from clipsvalorant.models import Match
from clipsvalorant.web import match_positions, match_rounds, match_scoreboard

MATCH_ID = "11111111-2222-3333-4444-555555555555"


def _payload(sample_payload, **over):
    data = json.loads(json.dumps(sample_payload["data"]))
    data.update(over)
    return data


# ------------------------------------------------------------- parsing

def test_a_kill_keeps_where_it_happened(sample_match):
    kill = sample_match.kills[0]
    assert kill.location.known
    assert (kill.location.x, kill.location.y) == (100, 200)


def test_a_kill_with_no_location_says_so_rather_than_guessing(sample_payload):
    data = _payload(sample_payload)
    for kill in data["kills"]:
        kill.pop("location", None)
    match = Match.model_validate(data)
    assert not match.kills[0].location.known
    assert match.kills[0].location.x is None


def test_the_map_keeps_its_uuid_as_well_as_its_name(sample_match):
    assert sample_match.metadata.map_name == "Ascent"
    assert sample_match.metadata.map_id == "m-ascent"


def test_an_agent_keeps_its_uuid_as_well_as_its_name(sample_match):
    player = sample_match.players[0]
    assert player.agent == "Jett"
    assert player.agent_id == "a-jett"


def test_a_bare_string_still_parses_where_a_pair_is_expected(sample_payload):
    """This API has returned maps and agents both ways between versions, and
    the whole point of the model being lenient is surviving that."""
    data = _payload(sample_payload)
    data["metadata"]["map"] = "Ascent"
    data["players"][0]["agent"] = "Jett"
    match = Match.model_validate(data)
    assert match.metadata.map_name == "Ascent"
    assert match.metadata.map_id == "Ascent"
    assert match.players[0].agent == "Jett"


def test_headshot_share_is_over_shots_landed(sample_match):
    stats = sample_match.players[0].stats
    assert stats.shots == 25
    assert stats.headshot_pct == pytest.approx(16.0)


def test_nobody_who_fired_nothing_gets_a_headshot_rate(sample_payload):
    """Zero of zero is not zero per cent, it is no answer - and a scoreboard
    reading 0% for a player who never shot is a lie about them."""
    data = _payload(sample_payload)
    data["players"][0]["stats"].update(headshots=0, bodyshots=0, legshots=0)
    assert Match.model_validate(data).players[0].stats.headshot_pct is None


# -------------------------------------------------------------- storing

@pytest.fixture
def stored(cfg, sample_match):
    me = sample_match.players[0]
    with db.session(cfg.db_path) as conn:
        persist_match(conn, sample_match, my_name=me.name, my_tag=me.tag,
                      region=cfg.region, raw=None)
        conn.commit()
    return cfg, me


def test_coordinates_reach_the_database(stored):
    cfg, _me = stored
    with db.session(cfg.db_path) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM kill WHERE x IS NOT NULL"
        ).fetchone()
    assert rows["n"] > 0


def test_the_scoreboard_comes_back_whole(stored):
    cfg, me = stored
    with db.session(cfg.db_path) as conn:
        board = match_scoreboard(conn, MATCH_ID)
    assert len(board) == 10
    mine = [p for p in board if p["mine"]]
    assert len(mine) == 1, "exactly one row is you"
    assert mine[0]["name"] == me.name
    assert mine[0]["agent_id"] == "a-jett"
    assert mine[0]["kills"] == 13
    assert mine[0]["hs"] == 16


def test_rounds_come_back_with_who_won_them(stored):
    cfg, _me = stored
    with db.session(cfg.db_path) as conn:
        rounds = match_rounds(conn, MATCH_ID)
    assert rounds, "no rounds were stored"
    first = rounds[min(rounds)]
    assert "won" in first and isinstance(first["won"], bool)
    assert first["winner"] in ("Red", "Blue", "")


def test_positions_are_labelled_by_whose_they_are(stored):
    """Your kills, the ones that killed you, and everyone else's are drawn
    differently, so mislabelling one is a visible lie about the match."""
    cfg, _me = stored
    with db.session(cfg.db_path) as conn:
        from clipsvalorant.assets import MapInfo
        info = MapInfo(uuid="m-ascent", name="Ascent", x_multiplier=1e-4,
                       y_multiplier=1e-4, x_scalar=0.5, y_scalar=0.5, has_icon=True)
        points = match_positions(conn, MATCH_ID, info)
    assert points
    kinds = {p["kind"] for p in points}
    assert kinds <= {"mine", "death", "other"}
    assert all(0.0 <= p["mx"] <= 1.0 and 0.0 <= p["my"] <= 1.0 for p in points)


def test_no_map_means_no_points_rather_than_points_at_the_origin(stored):
    cfg, _me = stored
    with db.session(cfg.db_path) as conn:
        assert match_positions(conn, MATCH_ID, None) == []


# ------------------------------------------------------------- backfill

def test_a_match_stored_whole_does_not_need_backfilling(stored):
    cfg, _me = stored
    with db.session(cfg.db_path) as conn:
        assert needs_backfill(conn) == []


def test_a_match_missing_its_map_uuid_is_offered_for_backfill(stored):
    """How a trimmed payload is recognised: the uuid arrives together with
    the coordinates and the damage, so if one is absent they all are."""
    cfg, _me = stored
    with db.session(cfg.db_path) as conn:
        conn.execute("UPDATE match SET map_id = NULL WHERE id = ?", (MATCH_ID,))
        conn.commit()
        assert needs_backfill(conn) == [MATCH_ID]


def test_the_payload_travels_with_the_parse(sample_payload):
    """What stops the trimming happening again: whatever stores a Match can
    reach the bytes it was parsed from, not merely the fields declared today.
    """
    data = sample_payload["data"]
    match = Match.model_validate(data).with_raw(data)
    assert match.raw is data
    assert "location" in json.dumps(match.raw)
