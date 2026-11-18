"""The collision regression.

A lobby-mate called Curry was matched to twitch.tv/curry, a radiant Valorant
streamer, on an exact name match plus a broadcast that happened to span the match.
It was the wrong person: the broadcast in question was a Deadlock stream, and the
real Curry was a friend with no Twitch channel. The rule that came out of it is
that a name and a broadcast clock can never confirm anything by themselves - only
evidence tying a channel to *this* match can.
"""

from clipsvalorant.models import Player
from clipsvalorant.resolve import (
    Evidence,
    _prefix_related,
    _same_id,
    _tags_in,
    normalise,
    riot_ids_in,
    vod_link,
)
from clipsvalorant.twitch import Vod


def _vod(created: int, duration: int, title: str = "") -> Vod:
    return Vod(
        id="1", channel_id="c", stream_id=None, title=title, created_at="",
        created_epoch=created, duration_s=duration, url="",
    )


# --- the verdict ladder -------------------------------------------------


def test_name_match_plus_a_broadcast_is_never_confirmed():
    """The exact shape of the curry mistake."""
    ev = Evidence(
        name_exact=True, vod_covers=True, match_len_s=1516, vod_overlap_s=1516,
        vod_title="SECRET DEADLOCK STREAM",
    )
    assert ev.verdict() == "unconfirmed"
    assert ev.confirmed_by() is None


def test_clips_inside_the_match_confirm():
    """What vaerie had: Valorant clips cut from that VOD, at offsets in the match."""
    ev = Evidence(
        name_prefix=True, vod_covers=True, match_len_s=1516, vod_overlap_s=1516,
        clips_on_vod=36, valorant_clips_on_vod=36, clips_inside_match=3,
    )
    assert ev.verdict() == "confirmed"
    assert ev.ties_to_this_match


def test_a_different_game_rules_a_channel_out():
    ev = Evidence(
        name_exact=True, vod_covers=True, match_len_s=1516, vod_overlap_s=1516,
        clips_on_vod=12, valorant_clips_on_vod=0,
    )
    assert ev.wrong_game
    assert ev.verdict() == "rejected"


def test_valorant_on_the_vod_but_not_in_the_match_is_only_likely():
    ev = Evidence(
        name_exact=True, vod_covers=True, match_len_s=1516, vod_overlap_s=1516,
        clips_on_vod=9, valorant_clips_on_vod=9, clips_inside_match=0,
    )
    assert ev.verdict() == "likely"


def test_an_advertised_riot_id_confirms_without_any_clips():
    ev = Evidence(
        name_exact=True, vod_covers=True, match_len_s=1516, vod_overlap_s=1516,
        advertised_ids=("PlayerOne#NA1",), id_confirms=True,
    )
    assert ev.verdict() == "confirmed"


def test_an_advertised_riot_id_belonging_to_someone_else_rules_out():
    ev = Evidence(
        name_exact=True, vod_covers=True, match_len_s=1516, vod_overlap_s=1516,
        advertised_ids=("SomeoneElse#1234",), id_conflicts=True,
    )
    assert ev.verdict() == "rejected"


def test_clip_evidence_outvotes_an_advertised_id():
    """A streamer with two accounts should not be rejected over the one in their bio."""
    ev = Evidence(
        vod_covers=True, match_len_s=1516, vod_overlap_s=1516, clips_inside_match=2,
        valorant_clips_on_vod=5, clips_on_vod=5,
        advertised_ids=("Smurf#9999",), id_conflicts=True,
    )
    assert ev.verdict() == "confirmed"


def test_no_usable_broadcast_is_rejected():
    assert Evidence(name_exact=True, match_len_s=1516, vod_overlap_s=0).verdict() == "rejected"
    # 40% of the match is not enough to be worth a link.
    assert Evidence(
        name_exact=True, match_len_s=1500, vod_overlap_s=600
    ).verdict() == "rejected"


def test_a_late_start_keeps_a_mostly_covering_vod():
    ev = Evidence(name_exact=True, match_len_s=1516, vod_overlap_s=1387, vod_late_start_s=129)
    assert ev.vod_usable
    assert "began 129s after the match did" in " ".join(ev.reasons())


# --- VOD arithmetic ----------------------------------------------------


def test_overlap_counts_only_the_shared_seconds():
    match_start, match_end = 1000, 2000
    assert _vod(900, 2000).overlap_s(match_start, match_end) == 1000   # spans it
    assert _vod(1200, 2000).overlap_s(match_start, match_end) == 800   # joined late
    assert _vod(900, 600).overlap_s(match_start, match_end) == 500     # left early
    assert _vod(5000, 600).overlap_s(match_start, match_end) == 0      # different day


def test_covers_is_stricter_than_overlap():
    vod = _vod(1200, 2000)
    assert vod.overlap_s(1000, 2000) == 800
    assert not vod.covers(1000, 2000, slack_s=45)


# --- the Riot ID harvest ------------------------------------------------


def test_riot_ids_survive_surrounding_prose():
    found = riot_ids_in("main acct PlayerOne#NA1, sub is vae#100")
    assert "PlayerOne#NA1" in found and "vae#100" in found


def test_hash_numbers_in_titles_are_not_riot_ids():
    assert riot_ids_in("FINDING #1 HUSH") == set()
    assert riot_ids_in("clip #42") == set()
    assert riot_ids_in("top 10 na radiant !hand") == set()


def test_an_id_matches_only_when_the_name_and_tag_both_agree():
    me = Player(puuid="p", name="PlayerOne", tag="NA1", team="Blue")
    assert _same_id("PlayerOne#NA1", me)
    assert _same_id("acct PlayerOne#NA1", me) is False
    assert _same_id("PlayerOne#9999", me) is False
    assert _same_id("Someone#NA1", me) is False


def test_conflict_is_judged_on_taglines():
    assert _tags_in(("Nope#1234", "sub acct PlayerOne#NA1")) == {"1234", "na1"}


# --- odds and ends ------------------------------------------------------


def test_prefix_relation_needs_three_characters():
    assert _prefix_related("vae", "vaerie")
    assert _prefix_related("vaerie", "vae")
    assert not _prefix_related("ab", "abcdef")
    assert not _prefix_related("zed", "vaerie")


def test_normalise_strips_everything_but_letters_and_digits():
    assert normalise("Bigger Boy") == "biggerboy"
    assert normalise("OutlawJC_") == "outlawjc"


def test_vod_links_never_go_negative():
    assert vod_link("7", -50).endswith("t=00h00m00s")
    assert vod_link("7", 3725).endswith("t=01h02m05s")
