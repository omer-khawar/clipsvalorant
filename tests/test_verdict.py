"""The collision regression.

A lobby-mate called Curry was matched to twitch.tv/curry, a radiant Valorant
streamer, on an exact name match plus a broadcast that happened to span the match.
It was the wrong person: the broadcast in question was a Deadlock stream, and the
real Curry was a friend with no Twitch channel. The rule that came out of it is
that a name and a broadcast clock can never confirm anything by themselves - only
evidence tying a channel to *this* match can.
"""

import dataclasses
import tempfile
from pathlib import Path

from clipsvalorant.models import Player
from clipsvalorant.resolve import (
    MIN_PREFIX_LEN,
    Evidence,
    _prefix_related,
    _same_id,
    _tags_in,
    bio_names,
    game_from_title,
    normalise,
    riot_ids_in,
    vod_link,
)
from clipsvalorant.twitch import Vod
from clipsvalorant.watch import CERTAIN_NAMES


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


def test_a_valorant_clip_inside_the_match_does_not_confirm():
    """twitch.tv/NeonQuin, reported as NeoN#Rage in a match it was never in.

    A clip cut from the covering VOD, filed under Valorant, at an offset inside
    the match says one thing: Valorant was on screen during those minutes. For
    a channel that streams Valorant daily that is true of every match its
    broadcast overlaps, so it cannot separate the player from a stranger. It is
    worth showing and it is not worth an alias.
    """
    ev = Evidence(
        name_prefix=True, vod_covers=True, match_len_s=1516, vod_overlap_s=1516,
        clips_on_vod=36, valorant_clips_on_vod=36, clips_inside_match=3,
    )
    assert ev.verdict() == "likely"
    assert not ev.ties_to_this_match


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


def test_only_the_screen_outvotes_an_advertised_id():
    """A streamer with two accounts should not be rejected over the one in
    their bio -- but it takes the lobby on screen to say so.

    Clips used to do this job and could not: they say Valorant was on, which
    an advertised Riot ID belonging to somebody else already implied.
    """
    clipped = Evidence(
        vod_covers=True, match_len_s=1516, vod_overlap_s=1516, clips_inside_match=2,
        valorant_clips_on_vod=5, clips_on_vod=5,
        advertised_ids=("Smurf#9999",), id_conflicts=True,
    )
    assert clipped.verdict() == "rejected"
    watched = dataclasses.replace(
        clipped, lobby_names_seen=tuple(str(i) for i in range(CERTAIN_NAMES))
    )
    assert watched.verdict() == "confirmed"


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


# --- the false-negative regression -------------------------------------
#
# klips found a streamer in a Sunset match that we reported as empty. It was
# CocaPup#slay -> twitch.tv/cocapuppy: a VOD titled "short valo stream!! !nt
# !lurk" covering the whole match, a bio reading "I am CocaPup!!", and nobody
# had ever clipped it. Two separate rules kept it invisible. Layer 1 demanded an
# exact name match, so no candidate was ever created; and confirmation demanded
# a clip, which a brand-new streamer will never have. Requiring clips does not
# just lose coverage, it biases the whole tool toward streamers with an audience.


def test_a_small_streamer_with_no_clips_reaches_the_page_but_not_the_alias_table():
    """A bio naming the player in plain words is the signal that reaches
    streamers nobody clips, and it is still only a resemblance plus a claim.

    `bio_names_player` is a substring of free text -- "I am Nuke!!" -- and the
    page is where it belongs. `id_confirms`, where the channel publishes the
    whole Riot ID including the tagline, is the one that confirms.
    """
    ev = Evidence(
        name_prefix=True, vod_covers=True, match_len_s=2348, vod_overlap_s=2348,
        clips_on_vod=0, bio_names_player=True, title_says_valorant=True,
    )
    assert ev.verdict() == "likely"
    assert dataclasses.replace(ev, id_confirms=True).verdict() == "confirmed"


def test_the_name_alone_is_still_not_enough():
    """Same channel shape, but the bio does not name them: not confirmed."""
    ev = Evidence(
        name_prefix=True, vod_covers=True, match_len_s=2348, vod_overlap_s=2348,
        title_says_valorant=True,
    )
    assert ev.verdict() == "likely"


def test_a_title_naming_another_game_rules_out_even_an_exact_name():
    ev = Evidence(
        name_exact=True, vod_covers=True, match_len_s=1516, vod_overlap_s=1516,
        title_says_other_game="deadlock",
    )
    assert ev.verdict() == "rejected"


def test_watching_valorant_is_not_playing_valorant():
    assert game_from_title("VCT OQ Watchparty") == (False, "watchparty")
    assert game_from_title("short valo stream!! !nt !lurk") == (True, "")
    assert game_from_title("SECRET DEADLOCK STREAM") == (False, "deadlock")


def test_a_bio_names_a_player_but_not_through_an_email():
    assert bio_names("WELCOME!!! I am CocaPup!! I am a new streamer", "CocaPup")
    # The one that would have re-confirmed the wrong curry.
    assert not bio_names("BUSINESS : curry@proxy.gg", "Curry")
    assert not bio_names("follow me at curry.gg for more", "Curry")
    assert not bio_names("stand on my money im 4'8", "vae")


def test_prefix_generation_needs_a_long_enough_name():
    """'Nuke' resembles hundreds of channels; 'CocaPup' resembles a handful."""
    assert _prefix_related("CocaPup", "cocapuppy", min_len=5)
    assert not _prefix_related("Nuke", "nuke73", min_len=5)


# --- the clip pool's own name bar ---------------------------------------

def test_the_clip_pool_uses_the_same_name_bar_as_the_name_search():
    """Four letters of a shared prefix is a coincidence, and it cost a result.

    `_prefix_related` defaults to three characters, which Layer 1 overrides to
    five and Layer 4 did not. So NeoN#Rage matched twitch.tv/NeonQuin - a
    channel that merely begins with those four letters - and because NeonQuin
    was being clipped on Valorant during the match, the candidate went straight
    to confirmed and the alias was written down. The frames said otherwise:
    NeonQuin was four rounds into a different game.
    """
    assert _prefix_related("NeoN", "neonquin")              # the old default
    assert not _prefix_related("NeoN", "neonquin", min_len=MIN_PREFIX_LEN)
    assert _prefix_related("Vaerie", "vae", min_len=MIN_PREFIX_LEN) is False
    assert _prefix_related("CocaPup", "cocapuppy", min_len=MIN_PREFIX_LEN)


def test_a_channel_from_the_clip_pool_needs_five_characters_to_be_tied_to_a_player():
    """The regression in one call, with nothing else in the lobby to decide it."""
    from clipsvalorant import db
    from clipsvalorant.resolve import ResolveReport, clip_derived_candidates

    short = Player(puuid="p1", name="NeoN", tag="Rage", team="Blue")
    long = Player(puuid="p2", name="CocaPup", tag="slay", team="Blue")

    class _Clips:
        def clips_in_window(self, *a, **k):
            return [
                {"broadcaster_id": "c1", "broadcaster_login": "neonquin",
                 "broadcaster_name": "NeonQuin"},
                {"broadcaster_id": "c2", "broadcaster_login": "cocapuppy",
                 "broadcaster_name": "cocapuppy"},
            ]

        def users(self, *, logins=None, ids=None):
            return []

    class _Match:
        id = "m1"

    with db.session(Path(tempfile.mkdtemp()) / "x.db") as conn:
        found = clip_derived_candidates(
            conn, _Clips(), _Match(), [short, long],
            0, 1800, "516575", ResolveReport(),
        )
    tied = {c.channel_login: c.player.name for c in found}
    assert tied == {"cocapuppy": "CocaPup"}


# --- re-reading what is already on disk ---------------------------------

def _encounter(conn, *, riot_id, login, verdict, evidence, channel_id="c1"):
    import json

    if conn.execute("SELECT 1 FROM match WHERE id = 'm1'").fetchone() is None:
        conn.execute(
            "INSERT INTO match (id, started_at, started_at_epoch, ingested_at,"
            " raw_json) VALUES ('m1','',0,'','{}')"
        )
    conn.execute(
        "INSERT INTO encounter (match_id, puuid, riot_id, channel_id, channel_login,"
        " vod_id, confidence, verified_how, verdict, evidence, found_at)"
        " VALUES ('m1','p',?,?,?,'v',0.95,'clip_window',?,?,'')",
        (riot_id, channel_id, login, verdict, json.dumps(evidence.__dict__)),
    )


def test_a_withdrawn_rule_is_applied_to_what_was_already_written_down():
    """The expensive half of a verdict is the evidence, and it is on the row.

    So a rule we have stopped believing can be withdrawn from the whole
    database for nothing - no Twitch call, no frame - which is the only way a
    match whose broadcasts Twitch has already deleted ever gets corrected.
    Without it, NeoN#Rage -> twitch.tv/NeonQuin would have been fixed on the
    18 November match and left standing on the 6 November one forever.
    """
    import tempfile
    from pathlib import Path

    from clipsvalorant import db
    from clipsvalorant.resolve import restate_verdicts

    ev = Evidence(
        name_prefix=True, vod_covers=True, match_len_s=1745, vod_overlap_s=1745,
        clips_on_vod=2, valorant_clips_on_vod=1, clips_inside_match=1,
    )
    path = Path(tempfile.mkdtemp()) / "x.db"
    with db.session(path) as conn:
        _encounter(conn, riot_id="NeoN#Rage", login="neonquin",
                   verdict="confirmed", evidence=ev)
        conn.execute(
            "INSERT INTO alias (riot_name, riot_tag, channel_id, source,"
            " confidence, confirmed_at) VALUES ('NeoN','Rage','c1','clip_window',0.95,'')"
        )
        out = restate_verdicts(conn)

        assert out.withdrawn == 1
        assert out.aliases_dropped == 1
        row = conn.execute("SELECT verdict FROM encounter").fetchone()
        # The prefix was never good enough either, so nothing is left of it.
        assert row["verdict"] == "unconfirmed"
        assert conn.execute("SELECT COUNT(*) AS n FROM alias").fetchone()["n"] == 0


def test_restating_leaves_a_confirmation_the_screen_paid_for_alone():
    import tempfile
    from pathlib import Path

    from clipsvalorant import db
    from clipsvalorant.resolve import restate_verdicts

    ev = Evidence(
        name_exact=True, vod_covers=True, match_len_s=1745, vod_overlap_s=1745,
        lobby_names_seen=tuple(str(i) for i in range(CERTAIN_NAMES)),
    )
    path = Path(tempfile.mkdtemp()) / "y.db"
    with db.session(path) as conn:
        _encounter(conn, riot_id="CocaPup#slay", login="cocapuppy",
                   verdict="confirmed", evidence=ev)
        out = restate_verdicts(conn)
        assert out.changed == 0
        assert conn.execute(
            "SELECT verdict FROM encounter"
        ).fetchone()["verdict"] == "confirmed"


def test_an_expired_broadcast_does_not_delete_a_confirmation():
    """Reported on razor#d4c: twitch.tv/cocapuppy was confirmed off ten of ten
    lobby names, and a later scan dropped it from the page.

    Nothing about the streamer had changed. Twitch had deleted the broadcast,
    so the re-run reached "no VOD holds enough of the match" and wrote that
    over the row. It is the one rejection every match earns eventually, it is
    a fact about retention rather than about the lobby, and the confirmation
    it overwrites can never be made again - the video is gone.
    """
    import tempfile
    from pathlib import Path

    from clipsvalorant import db
    from clipsvalorant.resolve import Candidate, _remember_encounter

    seen = Evidence(
        vod_covers=True, match_len_s=1745, vod_overlap_s=1745,
        lobby_names_seen=tuple(str(i) for i in range(CERTAIN_NAMES)),
    )
    player = Player(puuid="p", name="CocaPup", tag="slay", team="Blue")
    path = Path(tempfile.mkdtemp()) / "z.db"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO match (id, started_at, started_at_epoch, ingested_at,"
            " raw_json) VALUES ('m1','',0,'','{}')"
        )
        was = Candidate("m1", player, "c1", "cocapuppy", "cocapuppy",
                        "name_prefix", evidence=seen)
        _remember_encounter(conn, "m1", was, True)
        assert was.verdict == "confirmed"

        # The same channel, the same match, a fortnight later: the broadcast
        # is gone and nothing else was learned.
        expired = Candidate("m1", player, "c1", "cocapuppy", "cocapuppy",
                            "name_prefix",
                            evidence=Evidence(name_prefix=True, match_len_s=1745))
        assert expired.verdict == "rejected"
        _remember_encounter(conn, "m1", expired, True)
        assert conn.execute(
            "SELECT verdict FROM encounter"
        ).fetchone()["verdict"] == "confirmed"


def test_a_rejection_we_actually_learned_is_still_written_down():
    """The guard above must not turn into "confirmations are permanent". A
    broadcast we read and found a different game in is a real change of mind."""
    import tempfile
    from pathlib import Path

    from clipsvalorant import db
    from clipsvalorant.resolve import Candidate, _remember_encounter

    player = Player(puuid="p", name="NeoN", tag="Rage", team="Blue")
    path = Path(tempfile.mkdtemp()) / "w.db"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO match (id, started_at, started_at_epoch, ingested_at,"
            " raw_json) VALUES ('m1','',0,'','{}')"
        )
        _remember_encounter(conn, "m1", Candidate(
            "m1", player, "c1", "neonquin", "NeonQuin", "clip_window",
            evidence=Evidence(
                vod_covers=True, match_len_s=1745, vod_overlap_s=1745,
                lobby_names_seen=tuple(str(i) for i in range(CERTAIN_NAMES)))), True)
        later = Candidate("m1", player, "c1", "neonquin", "NeonQuin",
                          "clip_window",
                          evidence=Evidence(vod_covers=True, match_len_s=1745,
                                            vod_overlap_s=1745, frames_read=10,
                                            valorant_ui_frames=7,
                                            vod_other_match=True))
        assert later.verdict == "rejected"
        _remember_encounter(conn, "m1", later, True)
        assert conn.execute(
            "SELECT verdict FROM encounter"
        ).fetchone()["verdict"] == "rejected"
