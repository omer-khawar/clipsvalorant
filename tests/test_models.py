"""The models are deliberately permissive - these pin down that permissiveness."""

from clipsvalorant.models import Match, Metadata


def test_metadata_coerces_nested_objects(sample_match):
    meta = sample_match.metadata
    assert meta.match_id == "11111111-2222-3333-4444-555555555555"
    assert meta.map_name == "Ascent"          # from {"id":..., "name":"Ascent"}
    assert meta.queue == "Competitive"        # from a nested queue object
    assert meta.game_length_in_ms == 1_920_000
    assert meta.started_at.year == 2026


def test_metadata_accepts_bare_strings():
    meta = Metadata.model_validate(
        {
            "match_id": "x",
            "started_at": "2026-01-01T00:00:00Z",
            "map": "Haven",
            "queue": "unrated",
        }
    )
    assert meta.map_name == "Haven"
    assert meta.queue == "unrated"


def test_unknown_fields_are_ignored():
    """Schema drift must not crash the ingest."""
    meta = Metadata.model_validate(
        {
            "match_id": "x",
            "started_at": "2026-01-01T00:00:00Z",
            "some_field_henrik_added_last_week": {"nested": True},
        }
    )
    assert meta.match_id == "x"


def test_player_team_and_agent(sample_match):
    me = sample_match.find_me("TestMe", "0001")
    assert me is not None
    assert me.team == "Blue"
    assert me.agent == "Jett"
    assert me.stats.kills == 13


def test_find_me_is_case_insensitive(sample_match):
    assert sample_match.find_me("testme", "0001") is not None
    assert sample_match.find_me("TESTME", "0001") is not None
    assert sample_match.find_me("NotMe", "0001") is None


def test_roster_split(sample_match):
    me = sample_match.find_me("TestMe", "0001")
    assert len(sample_match.teammates_of(me.puuid)) == 4
    assert len(sample_match.opponents_of(me.puuid)) == 5


def test_kills_carry_millisecond_offsets(sample_match):
    assert len(sample_match.kills) == 68
    first = sample_match.kills[0]
    assert first.time_in_match_in_ms >= 0
    assert first.killer.puuid
    assert first.victim.puuid
    # The whole project hangs on this field existing and being monotonic.
    offsets = [k.time_in_match_in_ms for k in sample_match.kills]
    assert offsets == sorted(offsets)


def test_has_rounds_true_for_competitive(sample_match):
    assert sample_match.has_rounds is True


def test_has_rounds_false_for_deathmatch(dm_match):
    assert dm_match.has_rounds is False


def test_has_rounds_falls_back_to_round_count():
    """An unknown queue with a single round is treated as a respawn mode."""
    one_round = Match.model_validate(
        {
            "metadata": {
                "match_id": "x",
                "started_at": "2026-01-01T00:00:00Z",
                "queue": "some_new_mode_riot_added",
            },
            "players": [],
            "rounds": [{"id": 0}],
            "kills": [],
        }
    )
    assert one_round.has_rounds is False


def test_round_ceremony_lookup(sample_match):
    assert sample_match.round_ceremony(2) == "CeremonyAce"
    assert sample_match.round_ceremony(10) == "CeremonyCloser"
    assert sample_match.round_ceremony(999) is None
