"""Highlight detection. The fixture is built so every answer here is known."""

import pytest

from clipsvalorant.kills import find_highlights, kills_by_round


@pytest.fixture
def highlights(sample_match):
    me = sample_match.find_me("TestMe", "0001")
    return find_highlights(sample_match, me.puuid)


def by_round(highlights):
    return {(h.round, h.kind): h for h in highlights}


def test_ace_detected_in_round_2(highlights):
    found = by_round(highlights)[(2, "ace")]
    assert found.label == "Ace"
    assert found.kill_count == 5
    assert found.round_won is True
    # Round 2 starts at 200000ms; first kill is 10s in.
    assert found.time_in_match_ms == 210_000
    assert found.end_time_in_match_ms == 230_000


def test_ace_round_is_not_also_a_clutch(highlights):
    """All four teammates survived round 2, so no clutch may be reported."""
    assert (2, "clutch") not in by_round(highlights)


def test_triple_detected_in_round_4(highlights):
    found = by_round(highlights)[(4, "triple")]
    assert found.label == "3K"
    assert found.kill_count == 3


def test_clutch_detected_in_round_6(highlights):
    """Lost clutches get their own kind, matching tracker's clutchLost1v1."""
    found = by_round(highlights)[(6, "clutch_lost")]
    # No enemy died before I was left alone, so it is a 1v5, and we lost the round.
    assert found.label == "1v5 lost"
    assert found.kill_count == 2
    assert found.round_won is False
    assert found.time_in_match_ms == 630_000


def test_clutch_round_has_no_multikill(highlights):
    """Two kills is below the 3-kill multi threshold."""
    kinds = {kind for (rnd, kind) in by_round(highlights) if rnd == 6}
    assert kinds == {"clutch_lost"}


def test_no_clutch_when_you_died_first(highlights):
    """Round 8: I got two kills, then died, then my team died. Not a clutch."""
    assert (8, "clutch") not in by_round(highlights)
    assert (8, "clutch_lost") not in by_round(highlights)


def test_single_kill_round_is_not_a_highlight(highlights):
    assert not [h for h in highlights if h.round == 0]


def test_exact_highlight_set(highlights):
    assert by_round(highlights).keys() == {
        (2, "ace"),
        (4, "triple"),
        (6, "clutch_lost"),
        (10, "multi"),
        (12, "quadra"),
        (14, "triple"),
        (16, "clutch"),
        (18, "clutch"),
        (20, "clutch"),
    }


# --- regressions from real matches -----------------------------------------
# These three cases produced 15 false aces across 5 live matches before the
# detector counted distinct opponents instead of raw kills.


def test_six_kills_on_five_bodies_is_a_6k(highlights):
    """Round 10: six kills, five bodies (Sage rez).

    tracker.gg calls this a 6k, not an Ace - its tag is literally described as
    "Killed 6 enemies in one round", which five distinct enemies cannot produce.
    We keep the body count separately because it is the more meaningful number.
    """
    found = by_round(highlights)[(10, "multi")]
    assert found.label == "6K"
    assert found.kill_count == 6
    assert found.distinct_victims == 5


def test_multikill_in_a_lost_round_is_still_reported(highlights):
    """You can kill the whole enemy team and still lose to a detonation.

    Riot gives one ceremony per round and gives it to the winning team, so such
    a round carries CeremonyCloser rather than CeremonyAce. The ceremony is a
    corroborating signal, never the oracle.
    """
    found = by_round(highlights)[(10, "multi")]
    assert found.round_won is False
    assert found.ceremony == "CeremonyCloser"


def test_four_kills_on_three_bodies_is_a_4k(highlights):
    """Round 12: four kills, three bodies. tracker counts the kills."""
    found = by_round(highlights)[(12, "quadra")]
    assert found.label == "4K"
    assert found.kill_count == 4
    assert found.distinct_victims == 3


def test_teamkill_and_selfkill_do_not_count(highlights):
    """Round 14: three opponents, plus a teamkill and a self-kill.

    tracker's tags say "enemies", so neither may inflate the count.
    """
    found = by_round(highlights)[(14, "triple")]
    assert found.kill_count == 3
    assert found.distinct_victims == 3
    assert (14, "quadra") not in by_round(highlights)


# --- revives. Valorant has Sage's resurrect and Clove's self-revive, so "died
# once" is not "dead now". All three of these came from real matches where our
# output disagreed with tracker.gg, and tracker was right every time.


def test_revived_enemy_counts_as_alive(highlights):
    """Round 16: an enemy died, was revived, and killed my last teammate.

    Two enemies were up when I was left alone, so this is a 1v2.
    """
    found = by_round(highlights)[(16, "clutch")]
    assert found.label == "1v2 clutch"


def test_enemy_revived_then_killed_again_is_dead(highlights):
    """Round 18: revived, then died again before I was left alone.

    A blanket "revived means never dead" rule calls this a 1v2. It is a 1v1.
    """
    found = by_round(highlights)[(18, "clutch")]
    assert found.label == "1v1 clutch"


def test_your_own_revive_does_not_disqualify_you(highlights):
    """Round 20: I died early, was revived, and won the round alone.

    Treating my first death as final hid this clutch entirely.
    """
    found = by_round(highlights)[(20, "clutch")]
    assert found.label == "1v1 clutch"
    assert found.round_won is True


def test_revived_in_round_identifies_the_right_players(sample_match):
    from clipsvalorant.kills import kills_by_round, revived_in_round

    me = sample_match.find_me("TestMe", "0001")
    r20 = revived_in_round(kills_by_round(sample_match)[20])
    assert me.puuid in r20  # I was the one revived in round 20
    r18 = revived_in_round(kills_by_round(sample_match)[18])
    assert me.puuid not in r18


def test_respawn_modes_yield_no_highlights(dm_match):
    """Deathmatch: 12 kills in 'round 0' on three repeated victims."""
    me = dm_match.find_me("TestMe", "0001")
    assert dm_match.has_rounds is False
    assert find_highlights(dm_match, me.puuid) == []


def test_ceremony_is_recorded_when_present(highlights):
    assert by_round(highlights)[(2, "ace")].ceremony == "CeremonyAce"


def test_ace_is_five_kills(highlights):
    found = by_round(highlights)[(2, "ace")]
    assert found.label == "Ace"
    assert found.kill_count == 5
    assert found.distinct_victims == 5


def test_clutch_needs_no_minimum_kills(sample_match):
    """tracker's clutch tags fire on winning the round, not on a kill count."""
    from clipsvalorant.kills import MIN_MULTI_KILLS  # noqa: F401

    import clipsvalorant.kills as kills_mod

    assert not hasattr(kills_mod, "MIN_CLUTCH_KILLS")


def test_highlights_are_time_ordered(highlights):
    times = [h.time_in_match_ms for h in highlights]
    assert times == sorted(times)


def test_timestamp_formatting(highlights):
    assert by_round(highlights)[(2, "ace")].timestamp == "03:30"


def test_kills_by_round_groups_and_sorts(sample_match):
    grouped = kills_by_round(sample_match)
    assert set(grouped) == {0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20}
    for bucket in grouped.values():
        times = [k.time_in_match_in_ms for k in bucket]
        assert times == sorted(times)


def test_unknown_player_yields_nothing(sample_match):
    assert find_highlights(sample_match, "no-such-puuid") == []
