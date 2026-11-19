"""Layer 5: what the broadcast itself shows.

Every layer before this one builds a case out of circumstance - a name that
matches, a title that mentions the game, a stream that was running at the right
time. That case was wrong about Curry and it was silent about CocaPup, which is
the point: circumstance cuts both ways.

Valorant paints the lobby's names on the screen all match long. We know all ten
from the match history, so the question "was this broadcast showing our match?"
has a direct answer that owes nothing to inference. These tests pin down the
rules for reading it, and for what to do when the answer is no.
"""

from clipsvalorant.resolve import Evidence
from clipsvalorant.vision import FrameRead, TextBox, normalise, wanted_names
from clipsvalorant.watch import CERTAIN_NAMES, Watched, sample_offsets


def _frame(*texts: tuple[str, float, float], size: tuple[int, int] = (1280, 720)) -> FrameRead:
    boxes = tuple(TextBox(t, x, y, x + 50, y + 20) for t, x, y in texts)
    return FrameRead(boxes, size[0], size[1])


# --------------------------------------------------------------- reading names

def test_a_name_is_found_however_the_ocr_mangles_it():
    """The combat report glues the player's title onto their name, and the OCR
    then drops or invents a character at the join. Both of these came back from
    real frames for a single on-screen name."""
    names = wanted_names([("PlayerOne", True)])
    assert _frame(("TAG(PlayerOne", 1010, 306)).names_present(names) == {"PlayerOne"}
    assert _frame(("AGIPlayerOne", 1015, 354)).names_present(names) == {"PlayerOne"}


def test_a_name_too_short_is_never_searched_for():
    """A three-character name matches inside half the words on a busy screen -
    'ave' is in 'average', 'have', 'behaved'. Such names are dropped before we
    look, rather than explained away afterwards."""
    assert wanted_names([("ave", False)]) == {}
    assert wanted_names([("PlayerTwo", False)]) == {"playertwo": "PlayerTwo"}


def test_names_come_back_in_their_original_spelling():
    names = wanted_names([("two words", False)])
    assert _frame(("twowords", 10, 10)).names_present(names) == {"two words"}


# ------------------------------------------------------- telling games apart

def test_valorant_furniture_is_what_identifies_valorant():
    assert _frame(("COMBAT REPORT", 1095, 375)).looks_like_valorant
    assert _frame(("BUY PHASE", 555, 109)).looks_like_valorant
    assert not _frame(("DASH-JUMP: TOO LATE", 576, 587)).looks_like_valorant


def test_a_timer_at_the_top_is_not_enough_to_mean_valorant():
    """Deadlock draws a match timer within pixels of Valorant's. A frame that
    has only that is a frame we know nothing about."""
    deadlock = _frame(("7:05", 628, 9), ("$641 SOULS", 56, 590))
    assert deadlock.has_match_timer
    assert not deadlock.looks_like_valorant


def test_a_timer_somewhere_else_on_screen_is_not_a_match_timer():
    assert not _frame(("0:19", 20, 600)).has_match_timer
    assert _frame(("0:19", 614, 18)).has_match_timer


# ------------------------------------------------------- choosing the moments

def test_round_ends_are_sampled_before_mid_round_kills():
    """The round-end scoreboard shows all ten names at once; a killfeed shows
    two. Spend the first frame where it buys the most."""
    kills = [(1, 30.0), (1, 45.0), (2, 90.0), (2, 110.0)]
    first = sample_offsets(kills, limit=4)[0]
    assert first == 50.0  # last kill of round 1, plus the scoreboard lead


def test_sampling_spreads_over_the_whole_match():
    kills = [(r, r * 60.0 + i) for r in range(1, 25) for i in (0.0, 5.0)]
    picked = sample_offsets(kills, limit=6)
    assert len(picked) == 6
    assert max(picked) - min(picked) > 600


def test_sampling_is_confined_to_what_the_broadcast_covers():
    """A stream that joined twenty minutes in has no frames of the first round.
    Spending the budget seeking to them wastes the whole budget."""
    kills = [(1, 30.0), (5, 600.0), (9, 1200.0), (12, 1500.0)]
    picked = sample_offsets(kills, limit=6, window=(900.0, 1600.0))
    assert picked
    assert all(900.0 <= t <= 1610.0 for t in picked)


def test_a_window_holding_none_of_the_action_yields_nothing():
    assert sample_offsets([(1, 30.0), (2, 90.0)], window=(900.0, 1600.0)) == []


def test_no_kills_means_nowhere_worth_looking():
    assert sample_offsets([]) == []


# -------------------------------------------------------- ruling things out

def test_enough_of_the_lobby_on_screen_is_a_confirmation():
    ev = Evidence(vod_covers=True,
                  lobby_names_seen=tuple(str(i) for i in range(CERTAIN_NAMES)))
    assert ev.vod_shows_this_match
    assert ev.verdict() == "confirmed"


def test_a_couple_of_names_could_still_be_the_same_friend_group():
    """The same duo partners turn up across many of your matches, so two or
    three shared names prove nothing on their own."""
    ev = Evidence(vod_covers=True, lobby_names_seen=("PlayerOne", "PlayerTwo"))
    assert not ev.vod_shows_this_match


def test_looking_and_finding_nothing_outranks_a_perfect_circumstantial_case():
    """This is CocaPup's evidence exactly - the name fits, the bio names them,
    the broadcast is Valorant - and it would have been confirmed on that alone.
    Having actually watched the VOD and found no trace of the match, we don't."""
    ev = Evidence(vod_covers=True, name_prefix=True, bio_names_player=True,
                  title_says_valorant=True, frames_read=10,
                  vod_not_this_match=True)
    assert ev.verdict() == "rejected"


def test_a_stream_that_joined_late_is_still_confirmed_by_what_is_on_screen():
    """Coverage is a guess about what a broadcast contains; reading it is a
    measurement. A stream holding a third of the match fails the coverage
    test and can still plainly show the lobby, so the measurement wins."""
    ev = Evidence(vod_covers=False, vod_overlap_s=600, match_len_s=1800,
                  lobby_names_seen=tuple(str(i) for i in range(CERTAIN_NAMES)))
    assert not ev.vod_usable
    assert ev.verdict() == "confirmed"


def test_thin_coverage_with_nothing_on_screen_is_still_rejected():
    ev = Evidence(vod_covers=False, vod_overlap_s=600, match_len_s=1800)
    assert ev.verdict() == "rejected"


def test_a_vod_we_could_not_open_changes_nothing():
    """No OCR engine, an expired VOD, a subscriber-only broadcast: Layer 5 goes
    quiet and the layers above it decide as they did before."""
    ev = Evidence(vod_covers=True, name_prefix=True, bio_names_player=True,
                  title_says_valorant=True, frames_read=0)
    assert ev.verdict() == "confirmed"


def test_direct_evidence_still_wins_when_the_screen_is_unreadable():
    ev = Evidence(vod_covers=True, clips_inside_match=2, frames_read=0)
    assert ev.verdict() == "confirmed"


def test_seeing_the_lobby_counts_as_tying_to_this_match():
    ev = Evidence(vod_covers=True,
                  lobby_names_seen=tuple(str(i) for i in range(CERTAIN_NAMES)))
    assert ev.ties_to_this_match
    assert "appear by name in the VOD" in (ev.confirmed_by() or "")


# ---------------------------------------------- the candidate's own name

def test_the_candidates_own_name_is_not_evidence_about_the_match():
    """A channel is a candidate because its name resembles the player's, so its
    own watermark names something very like that player by construction.

    Measured on four broadcasts we had correctly ruled out, every single one
    returned exactly one "lobby name" this way: twitch.tv/curry showing CURRY,
    twitch.tv/rogue showing Rogue, sacred showing their Apex name sacred_NRG.
    That is the candidate agreeing with itself. Counting it would put every
    name-matched channel one weak step away from being confirmed.
    """
    from clipsvalorant.watch import _normalise_key
    lobby = wanted_names([("Curry", False), ("Juneha", False), ("CREED", False)])
    kept = {k: v for k, v in lobby.items() if k != _normalise_key("Curry")}
    assert "curry" not in kept
    assert _frame(("CURRYO", 50, 50)).names_present(kept) == set()
    # the other players carry no such circularity
    assert _frame(("Juneha", 50, 50)).names_present(kept) == {"Juneha"}


def test_excluding_one_name_leaves_a_real_confirmation_standing():
    """CocaPup's broadcast returned all ten lobby names. Dropping their own
    still leaves nine, which is the point: real evidence does not depend on
    the one name that was never independent."""
    seen = {"CocaPup", "Georgy", "Nuke", "Razer", "Scarecrow",
            "heyyharvey", "resticle", "Bigger", "Inferno"}
    assert len(seen - {"CocaPup"}) >= CERTAIN_NAMES


# ------------------------------------------------------------ the watch result

def test_a_few_frames_are_not_enough_to_rule_anything_out():
    assert not Watched(frames_read=2).saw_nothing


def test_a_timer_alone_does_not_save_a_broadcast_from_being_ruled_out():
    """Curry's VOD: ten frames read, a timer in nine of them, and not one
    Valorant word or lobby name. That is Deadlock, and we can say so."""
    assert Watched(frames_read=10, timer_frames=9, valorant_frames=0).saw_nothing


def test_valorant_on_screen_stops_us_ruling_it_out():
    """It is Valorant and we could not read the names - maybe an overlay, maybe
    the streamer was in a menu. Not confirmed, but not denied either."""
    assert not Watched(frames_read=10, valorant_frames=4).saw_nothing


def test_certainty_needs_the_whole_threshold():
    assert Watched(names=set(str(i) for i in range(CERTAIN_NAMES))).certain
    assert not Watched(names=set(str(i) for i in range(CERTAIN_NAMES - 1))).certain


def test_an_unreadable_frame_is_never_an_error(tmp_path):
    """Failing to read a frame is the commonest outcome in the whole program -
    expired VODs, subscriber-only broadcasts, a seek past the end. It happened
    to crash a run instead: on Windows the file ffmpeg abandoned was still held
    open, and deleting it raised PermissionError out of the frame reader."""
    from clipsvalorant.vod import FrameReader
    reader = FrameReader(tmp_path)
    empty = tmp_path / "held.png"
    empty.write_bytes(b"too small")
    assert FrameReader._keep_if_real(empty) is None
    assert FrameReader._keep_if_real(tmp_path / "never_written.png") is None


def test_a_real_frame_survives_the_check(tmp_path):
    from clipsvalorant.vod import FrameReader
    good = tmp_path / "good.png"
    good.write_bytes(b"x" * 2048)
    assert FrameReader._keep_if_real(good) == good


def test_being_live_on_valorant_is_a_game_check_a_title_cannot_fake():
    """One broadcast in the sample was titled "VCT OQ Watchparty" while showing
    Deadlock. /helix/streams reports the game id itself, so for a match that has
    only just ended it answers the question the title was guessing at."""
    ev = Evidence(vod_covers=True, name_prefix=True, live_on_valorant=True)
    assert ev.looks_like_valorant
    assert ev.verdict() == "likely"


def test_being_live_is_still_not_evidence_about_this_match():
    """Live on Valorant now says nothing about who was in that lobby an hour
    ago, so on its own it never confirms."""
    ev = Evidence(vod_covers=True, live_on_valorant=True)
    assert ev.verdict() == "unconfirmed"


def test_normalise_strips_what_ocr_invents():
    assert normalise("TAG | PlayerOne") == "tagplayerone"
    assert normalise("COMBAT REPORT") == "combatreport"
