"""Putting a kill on a minimap, and refusing to when we cannot.

The conversion is four numbers from Riot's own asset service, and it has one
trap in it: the game's y drives the image's x. Get that the wrong way round
and you still get a map covered in plausible dots, every one of them wrong -
which is the worst kind of bug this project can ship, because the whole point
of the thing is that its timestamps and positions are true.

The other half of this file is about refusing. A map with no multipliers, a
coordinate from off the edge of the world, an id that is really a path - all
of them end as "no dot" rather than a guess.
"""

import json

import pytest

from clipsvalorant.assets import Assets, MapInfo


# Abyss's real numbers, read from valorant-api.
ABYSS = MapInfo(
    uuid="224b0a95-48b9-f703-1bd8-67aca101a61f", name="Abyss",
    x_multiplier=8.1e-05, y_multiplier=-8.1e-05,
    x_scalar=0.5, y_scalar=0.5, has_icon=True,
)


def test_the_axes_cross_over():
    """The game's y drives the image's x. This is the whole conversion.

    Checked with a point on one axis only: if the two were applied straight
    across, this would move the other way and every dot on every map would be
    mirrored into a wrong room.
    """
    on_y = ABYSS.point(0, 4000)
    on_x = ABYSS.point(4000, 0)
    assert on_y[0] > 0.5 and on_y[1] == pytest.approx(0.5)
    assert on_x[1] < 0.5 and on_x[0] == pytest.approx(0.5)


def test_the_centre_of_the_world_is_the_centre_of_the_image():
    assert ABYSS.point(0, 0) == pytest.approx((0.5, 0.5))


def test_a_real_kill_lands_on_the_map():
    """One of ours, off a Corrode match: inside the image, not at its edge."""
    x, y = ABYSS.point(2773, 1880)
    assert 0.0 < x < 1.0 and 0.0 < y < 1.0


def test_a_coordinate_off_the_edge_of_the_world_is_refused():
    """Better no dot than one pinned to the border pretending to be a place."""
    assert ABYSS.point(9_999_999, 0) is None
    assert ABYSS.point(0, -9_999_999) is None


def test_a_map_with_no_multipliers_draws_nothing():
    """Zero multipliers would put every kill of the match on one point, which
    looks like a working feature and is not."""
    blank = MapInfo(uuid="u", name="Nowhere", has_icon=True)
    assert not blank.usable
    assert blank.point(100, 200) is None


def test_a_map_with_no_artwork_is_not_usable():
    """The dots are positioned against the image; without it there is nothing
    for them to be positioned against."""
    no_art = MapInfo(uuid="u", name="Hidden", x_multiplier=1e-4, y_multiplier=1e-4,
                     has_icon=False)
    assert not no_art.usable


# ---------------------------------------------------------------- lookups

def _cached(tmp_path):
    """An Assets whose catalogue is already on disk, so nothing is fetched."""
    store = Assets(tmp_path)
    store._catalogue_path.write_text(json.dumps({
        "maps": [{
            "uuid": ABYSS.uuid, "name": "Abyss",
            "x_multiplier": ABYSS.x_multiplier, "y_multiplier": ABYSS.y_multiplier,
            "x_scalar": 0.5, "y_scalar": 0.5, "has_icon": True,
        }],
        "agents": {"a-jett": "Jett"},
    }), encoding="utf-8")
    return store


def test_a_map_is_found_by_uuid(tmp_path):
    assert _cached(tmp_path).map_for(ABYSS.uuid).name == "Abyss"


def test_a_map_is_found_by_name_for_matches_stored_before_uuids(tmp_path):
    """Sixty-seven matches here predate the uuid being kept. Falling back to
    the name is what lets them draw a map at all."""
    assert _cached(tmp_path).map_for(None, "abyss").uuid == ABYSS.uuid


def test_an_unknown_map_is_simply_unknown(tmp_path):
    store = _cached(tmp_path)
    assert store.map_for("not-a-map") is None
    assert store.map_for(None, "Atlantis") is None
    assert store.map_for(None, None) is None


def test_an_id_that_is_really_a_path_is_refused(tmp_path):
    """Ids arrive over HTTP from the page, so they are checked rather than
    trusted: this is what stops a crafted request naming a file outside the
    cache directory."""
    store = _cached(tmp_path)
    for nasty in ("../../etc/passwd", "..\\..\\windows\\system32", "a/b",
                  "", "x" * 80, "name with spaces"):
        assert store.image("map", nasty) is None


def test_an_unknown_kind_is_refused(tmp_path):
    assert _cached(tmp_path).image("shell", ABYSS.uuid) is None


def test_an_image_already_cached_is_served_without_a_fetch(tmp_path, monkeypatch):
    store = _cached(tmp_path)
    dest = tmp_path / f"map-{ABYSS.uuid}.png"
    dest.write_bytes(b"x" * 2048)

    def explode(*a, **k):
        raise AssertionError("went to the network for a file it already had")

    monkeypatch.setattr("httpx.Client", explode)
    assert store.image("map", ABYSS.uuid) == dest


def test_a_missing_catalogue_is_survivable(tmp_path, monkeypatch):
    """No internet is not an error here. The page draws names instead of
    pictures, and every timestamp still works."""
    def explode(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr("httpx.Client", explode)
    store = Assets(tmp_path)
    assert store.maps() == {}
    assert store.map_for("anything") is None
