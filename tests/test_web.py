"""The web UI: the clip list it builds, and the endpoints it serves.

Two things here are worth guarding closely.

The first is the offset arithmetic. A link into a broadcast is the entire
product, and both ways of getting it wrong fail silently: a negative offset
points at the start of the stream, and one past the end gets the last frame
back from Twitch with no error. Either looks like the detector was wrong
rather than like the stream not containing the moment.

The second is that a Riot ID typed into a browser becomes a filename. Nothing
else in this project takes a path from untrusted input.
"""

import dataclasses
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from clipsvalorant import calibrate, db, web

MATCH_START = 1_700_000_000
VOD_START = MATCH_START - 600  # the stream began ten minutes before the match

# For anything that exercises the day window. MATCH_START is a fixed point in
# the past, which is what makes the clip-building tests reproducible, but it
# is years outside any retention window - so a search test needs a match that
# is actually recent.
RECENT = int(time.time()) - 3600


@pytest.fixture()
def conn(tmp_path):
    with db.session(tmp_path / "t.db") as c:
        yield c


def add_everything(
    conn,
    *,
    verdict="confirmed",
    vod_start=VOD_START,
    duration=7200,
    at_ms=(120_000,),
    evidence=None,
    channel="streamer",
    match_id="m1",
    shift_s=None,
):
    """One match, one covering broadcast, one encounter, and some highlights."""
    conn.execute(
        "INSERT INTO match (id, started_at, started_at_epoch, ingested_at, raw_json, "
        "map, mode) VALUES (?,?,?,?,?,?,?)",
        (match_id, "", MATCH_START, "", "{}", "Ascent", "Competitive"),
    )
    for i, ms in enumerate(at_ms):
        conn.execute(
            "INSERT INTO highlight (match_id, round, kind, label, kill_count, "
            "time_in_match_ms, end_time_in_match_ms, round_won) VALUES (?,?,?,?,?,?,?,?)",
            (match_id, i, "triple", "3K", 3, ms, ms + 5000, 1),
        )
    vod_id = f"vod-{channel}"
    conn.execute(
        "INSERT OR REPLACE INTO twitch_vod (id, channel_id, title, created_at, "
        "created_epoch, duration_s, end_epoch, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
        (vod_id, "c1", "valorant ranked", "", vod_start, duration,
         vod_start + duration, ""),
    )
    conn.execute(
        "INSERT INTO encounter (match_id, puuid, riot_id, channel_id, channel_login, "
        "vod_id, same_team, confidence, verified_how, verdict, evidence, found_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (match_id, f"p-{channel}", "Them#0001", "c1", channel, vod_id, 0, 0.9,
         "name_match", verdict,
         json.dumps(evidence) if evidence is not None else None, ""),
    )
    if shift_s is not None:
        conn.execute(
            "INSERT OR REPLACE INTO match_vod_offset (match_id, vod_id, shift_s, "
            "method, votes, measured_at) VALUES (?,?,?,?,?,?)",
            (match_id, vod_id, shift_s, "measured", 6, ""),
        )
    conn.commit()


# ------------------------------------------------------------ names and paths

@pytest.mark.parametrize("raw", ["PlayerOne#NA1", " PlayerOne # NA1 ", "a#b"])
def test_split_riot_id_accepts_both_halves(raw):
    name, tag = web.split_riot_id(raw)
    assert name and tag and "#" not in tag


@pytest.mark.parametrize("raw", ["", "PlayerOne", "#NA1", "   ", "PlayerOne#"])
def test_split_riot_id_rejects_half_an_id(raw):
    with pytest.raises(ValueError):
        web.split_riot_id(raw)


@pytest.mark.parametrize(
    "raw",
    ["../../etc/passwd", "..\\..\\windows\\system32", "a/b/c", "C:\\x", "....//"],
)
def test_slug_cannot_escape_a_directory(raw):
    assert "/" not in web.slug(raw)
    assert "\\" not in web.slug(raw)
    assert ".." not in web.slug(raw)


def test_slug_never_empty():
    assert web.slug("###") == "unnamed"


def test_config_for_own_account_keeps_the_main_database(cfg):
    same = web.config_for(cfg, f"{cfg.riot_name}#{cfg.riot_tag}")
    assert same.db_path == cfg.db_path


def test_config_for_someone_else_gets_its_own_database(cfg):
    """Two accounts in one file would fight over `is_me`.

    Highlights are derived at ingest time from which player is "me", so a
    second account sharing the database would silently inherit the first
    account's plays.
    """
    other = web.config_for(cfg, "Someone#ELSE")
    assert other.db_path != cfg.db_path
    assert other.db_path.parent == cfg.db_path.parent
    assert other.riot_name == "Someone" and other.riot_tag == "ELSE"


def test_config_for_traversal_stays_in_the_data_directory(cfg):
    evil = web.config_for(cfg, "../../etc/passwd#x")
    assert evil.db_path.parent == cfg.db_path.parent


def test_path_for_key_refuses_a_key_it_did_not_list(tmp_path):
    assert web.path_for_key(tmp_path, "../../secrets") is None
    assert web.path_for_key(tmp_path, "nothing-here") is None


def test_known_accounts_does_not_write(tmp_path):
    """Listing is read-only, so a browse never migrates somebody's database."""
    path = tmp_path / "acct.db"
    with db.session(path) as conn:
        db.upsert_account(conn, puuid="p", name="Me", tag="0001",
                          region="na", is_me=True)
    before = path.stat().st_mtime_ns
    assert web.known_accounts(tmp_path)[0]["riot_id"] == "Me#0001"
    assert path.stat().st_mtime_ns == before


def test_known_accounts_skips_files_that_are_not_ours(tmp_path):
    (tmp_path / "junk.db").write_bytes(b"not a database")
    assert web.known_accounts(tmp_path) == []


# ------------------------------------------------------------ the clip list

def test_offset_uses_the_measured_start_of_the_match(conn):
    """The arithmetic that was wrong for the whole life of the project.

    A match's own timestamp is the queue popping; its in-match clock starts
    when the map loads. Adding them together ignores agent select and skips
    back about a round and a half.
    """
    add_everything(conn, at_ms=(120_000,), shift_s=100.0)
    clip = web.clip_groups(conn)[0]["clips"][0]
    # 600s into the broadcast, 100s of agent select and loading, 120s into
    # the match, 8s of lead-in.
    assert clip["offset"] == 600 + 100 + 120 - web.LEAD_S
    assert clip["stamp"] == "00h13m32s"
    assert clip["playable"]


def test_offset_falls_back_to_the_median_when_nobody_measured(conn):
    add_everything(conn, at_ms=(120_000,))
    group = web.clip_groups(conn)[0]
    assert group["timing"] == "estimated"
    assert group["clips"][0]["offset"] == (
        600 + int(calibrate.FALLBACK_SHIFT_S) + 120 - web.LEAD_S
    )


def test_a_measured_group_says_so(conn):
    add_everything(conn, shift_s=97.0)
    group = web.clip_groups(conn)[0]
    assert group["timing"] == "measured" and group["shift_s"] == 97.0


def test_lead_is_subtracted_not_added(conn):
    """A kill ends a play. Landing on the timestamp arrives after the good part."""
    add_everything(conn, at_ms=(120_000,))
    plain = web.clip_groups(conn, lead=0)[0]["clips"][0]["offset"]
    led = web.clip_groups(conn, lead=10)[0]["clips"][0]["offset"]
    assert led == plain - 10


def test_moment_before_the_stream_started_is_not_playable(conn):
    # The broadcast opens five minutes into the match; the highlight is at 2:00.
    add_everything(conn, vod_start=MATCH_START + 300, at_ms=(120_000,))
    clip = web.clip_groups(conn)[0]["clips"][0]
    assert not clip["playable"]
    assert "began" in clip["why_not"]


def test_moment_after_the_stream_ended_is_not_playable(conn):
    """Twitch answers an over-long offset with the last frame and no error.

    Which is why this is checked here rather than left to the player: a link
    that lands on the end card is indistinguishable from a wrong detection.
    """
    add_everything(conn, duration=700, at_ms=(600_000,))
    clip = web.clip_groups(conn)[0]["clips"][0]
    assert not clip["playable"]
    assert "ended" in clip["why_not"]


def test_unplayable_moments_are_still_listed(conn):
    """A streamer who joined late is a real find; showing nothing hides it."""
    add_everything(conn, vod_start=MATCH_START + 300, at_ms=(120_000, 600_000))
    group = web.clip_groups(conn)[0]
    assert len(group["clips"]) == 2
    assert group["playable"] == 1


def test_rejected_encounters_never_reach_the_page(conn):
    add_everything(conn, verdict="rejected")
    assert web.clip_groups(conn) == []


def test_confirmed_sorts_above_likely(conn):
    add_everything(conn, channel="maybe", verdict="likely", match_id="m1")
    add_everything(conn, channel="sure", verdict="confirmed", match_id="m2")
    assert [g["verdict"] for g in web.clip_groups(conn)] == ["confirmed", "likely"]


def test_unconfirmed_never_reaches_the_page(conn):
    """An unconfirmed row is the absence of a judgement, not a weak one.

    Nothing says the broadcast is even Valorant. Giving it timestamps makes
    it indistinguishable on screen from a streamer we actually found, which
    is how twitch.tv/solly17x came to be listed with five playable moments
    of a stream that was Elden Ring from end to end.
    """
    add_everything(conn, verdict="unconfirmed")
    assert web.clip_groups(conn) == []


def test_one_group_per_streamer_per_match(conn):
    add_everything(conn, channel="one", match_id="m1", at_ms=(1000, 2000))
    add_everything(conn, channel="two", match_id="m2", at_ms=(1000,))
    groups = web.clip_groups(conn)
    assert sorted(len(g["clips"]) for g in groups) == [1, 2]


def test_an_encounter_with_no_vod_is_not_a_clip(conn):
    add_everything(conn)
    conn.execute("UPDATE encounter SET vod_id = NULL")
    assert web.clip_groups(conn) == []


# ------------------------------------------------------------ evidence

def test_evidence_becomes_sentences(conn):
    add_everything(conn, evidence={"name_exact": True, "vod_covers": True})
    reasons = web.clip_groups(conn)[0]["reasons"]
    assert any("matches the Riot ID exactly" in r for r in reasons)


def test_evidence_survives_a_field_this_version_does_not_know(conn):
    """Rows outlive the code that wrote them.

    A field added and later removed would otherwise take the whole explanation
    down with it, leaving a verdict on screen with nothing supporting it.
    """
    add_everything(conn, evidence={"name_exact": True, "invented_later": 7})
    assert web.clip_groups(conn)[0]["reasons"]


def test_evidence_tuple_fields_round_trip_through_json(conn):
    add_everything(conn, evidence={"lobby_names_seen": ["a", "b", "c", "d"]})
    reasons = web.clip_groups(conn)[0]["reasons"]
    assert any("4 of this lobby's players are named" in r for r in reasons)


@pytest.mark.parametrize("blob", [None, "", "not json", "[1,2,3]", "null"])
def test_unreadable_evidence_costs_only_the_explanation(conn, blob):
    add_everything(conn)
    conn.execute("UPDATE encounter SET evidence = ?", (blob,))
    groups = web.clip_groups(conn)
    assert len(groups) == 1 and groups[0]["reasons"] == []


# ------------------------------------------------------------ the server

class _Harness:
    """The real handler on a real socket, with the search replaced by a stub."""

    def __init__(self, cfg):
        self.started = []
        state = web.State(cfg=cfg, runner=self._fake_search)
        self.state = state
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(state))
        self.port = self.server.server_address[1]
        self.release = threading.Event()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _fake_search(self, cfg, job, **kwargs):
        self.started.append((job.riot_id, kwargs))
        job.say("stub running")
        self.release.wait(timeout=5)
        job.state = "done"
        job.finished = time.time()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
            return r.status, json.loads(r.read() or b"{}")

    def post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def srv(cfg):
    with db.session(cfg.db_path) as conn:
        db.upsert_account(conn, puuid="me", name=cfg.riot_name, tag=cfg.riot_tag,
                          region=cfg.region, is_me=True)
        add_everything(conn)
    harness = _Harness(cfg)
    try:
        yield harness
    finally:
        harness.close()


def test_index_is_html(srv):
    with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/") as r:
        body = r.read().decode("utf-8")
    assert r.status == 200
    assert body.startswith("<!doctype html>")
    assert "player.twitch.tv" in body


def test_state_lists_the_account(srv, cfg):
    status, data = srv.get("/api/state")
    assert status == 200
    assert data["default_riot_id"] == cfg.riot_id
    assert any(a["riot_id"] == cfg.riot_id for a in data["accounts"])


def test_state_never_carries_a_credential(srv):
    """The page is a browser away from the clipboard; keys have no business in it."""
    _, data = srv.get("/api/state")
    assert "has_twitch" in data  # whether, not what
    flat = json.dumps(data)
    assert "client_secret" not in flat and "api_key" not in flat


def test_clips_defaults_to_the_configured_account(srv):
    status, data = srv.get("/api/clips")
    assert status == 200
    assert len(data["groups"]) == 1
    assert data["groups"][0]["channel"] == "streamer"


def test_clips_for_an_unknown_account_explains_itself(srv):
    status, data = srv.get("/api/clips?key=nobody")
    assert status == 200 and data["groups"] == [] and data["error"]


def test_search_starts_a_job_and_passes_the_options_through(srv):
    status, job = srv.post("/api/search", {
        "riot_id": "Other#0002", "days": 3, "mode": "unrated", "watch": False,
    })
    assert status == 202 and job["state"] == "running"
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.02)
    riot_id, kwargs = srv.started[0]
    assert riot_id == "Other#0002"
    assert kwargs["days"] == 3 and kwargs["mode"] == "unrated"
    assert kwargs["watch"] is False


def test_a_search_looks_back_a_fortnight_by_default(srv):
    """Fourteen days is the whole of what Twitch still holds.

    A shorter default would quietly skip matches that are still findable; a
    longer one would spend API calls on broadcasts that have been deleted.
    """
    srv.post("/api/search", {"riot_id": "Other#0002"})
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.02)
    assert srv.started[0][1]["days"] == web.DEFAULT_DAYS == 14


def test_search_clamps_an_absurd_window(srv):
    srv.post("/api/search", {"riot_id": "Other#0002", "days": 10_000})
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.02)
    assert srv.started[0][1]["days"] == web.MAX_DAYS


def test_any_mode_means_no_filter(srv):
    srv.post("/api/search", {"riot_id": "Other#0002", "mode": "any"})
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.02)
    assert srv.started[0][1]["mode"] is None


def test_a_second_search_is_refused_while_one_runs(srv):
    """Serialised on purpose: two searches would race for one API quota."""
    srv.post("/api/search", {"riot_id": "Other#0002"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        srv.post("/api/search", {"riot_id": "Third#0003"})
    assert exc.value.code == 409


def test_half_a_riot_id_is_refused_before_anything_starts(srv):
    with pytest.raises(urllib.error.HTTPError) as exc:
        srv.post("/api/search", {"riot_id": "NoTagHere"})
    assert exc.value.code == 400
    assert srv.started == []


def test_unknown_routes_are_404(srv):
    for path in ("/api/nope", "/../etc/passwd"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            srv.get(path)
        assert exc.value.code == 404


def test_job_endpoint_is_idle_before_anything_runs(srv):
    _, data = srv.get("/api/job")
    assert data["state"] == "idle"


# ------------------------------------------------------------ job plumbing

def test_job_log_is_capped():
    job = web.Job(id="j", riot_id="a#b", key="k")
    for i in range(web.MAX_JOB_LINES + 50):
        job.say(str(i))
    lines = job.snapshot()["lines"]
    assert len(lines) == web.MAX_JOB_LINES
    assert lines[-1] == str(web.MAX_JOB_LINES + 49)  # the newest survive


def test_a_failed_search_leaves_the_job_in_error(cfg, monkeypatch):
    """The thread is the last place an exception can be caught.

    Anything that escapes here is lost with the thread, and the page spins
    forever on a job that is no longer running.
    """
    def boom(*a, **k):
        raise RuntimeError("henrik is down")

    monkeypatch.setattr("clipsvalorant.ingest.ingest", boom)
    job = web.Job(id="j", riot_id="Someone#ELSE", key="")
    web.run_search(cfg, job, days=1, watch=False)
    assert job.state == "error"
    assert "henrik is down" in job.error
    assert job.finished > 0


def test_search_stops_politely_without_twitch_credentials(cfg, monkeypatch):
    from clipsvalorant.ingest import IngestReport

    monkeypatch.setattr("clipsvalorant.ingest.ingest",
                        lambda c, **k: IngestReport(seen=1, stored=1))
    other = web.config_for(cfg, "Someone#ELSE")
    with db.session(other.db_path) as conn:
        conn.execute(
            "INSERT INTO match (id, started_at, started_at_epoch, ingested_at, "
            "raw_json) VALUES (?,?,?,?,?)", ("m1", "", RECENT, "", "{}"))
    job = web.Job(id="j", riot_id="Someone#ELSE", key="")
    web.run_search(cfg, job, days=14, watch=False)
    assert job.state == "done"
    assert any("Twitch credentials" in line for line in job.snapshot()["lines"])


def test_a_match_older_than_the_window_is_never_resolved(cfg, monkeypatch):
    """Resolving it would spend API calls on a broadcast Twitch has deleted.

    The user is told why rather than shown an empty result, because "nothing
    found" and "nothing left to find" look identical and mean very different
    things.
    """
    from clipsvalorant.ingest import IngestReport

    monkeypatch.setattr("clipsvalorant.ingest.ingest",
                        lambda c, **k: IngestReport(seen=1, stored=1))
    called = []
    monkeypatch.setattr("clipsvalorant.resolve.resolve_recent",
                        lambda *a, **k: called.append(k) or _NoReport())
    other = web.config_for(cfg, "Someone#ELSE")
    with db.session(other.db_path) as conn:
        conn.execute(
            "INSERT INTO match (id, started_at, started_at_epoch, ingested_at, "
            "raw_json) VALUES (?,?,?,?,?)",
            ("old", "", RECENT - 40 * 86400, "", "{}"))
    job = web.Job(id="j", riot_id="Someone#ELSE", key="")
    web.run_search(cfg, job, days=14, watch=False)
    assert job.state == "done"
    assert called == []  # never asked Twitch about a dead broadcast
    lines = " ".join(job.snapshot()["lines"])
    assert "Nothing inside the last 14 days" in lines
    assert "40 days old" in lines


class _NoReport:
    confirmed: tuple = ()
    candidates: tuple = ()
    errors: tuple = ()

    def summary(self) -> str:
        return "nothing"


def test_progress_counts_matches_as_they_are_checked(cfg, monkeypatch):
    """The bar is driven by a count, not by the log.

    A narration line and a position are different things, and parsing the
    former to produce the latter is how a progress bar comes to disagree with
    the work it is reporting.
    """
    from clipsvalorant.ingest import IngestReport

    monkeypatch.setattr("clipsvalorant.ingest.ingest",
                        lambda c, **k: IngestReport(seen=2, stored=2))

    def fake_resolve(acct, *, match_ids, watch_vods, on_match):
        for mid in match_ids:
            on_match(mid, _NoReport())
        return _NoReport()

    monkeypatch.setattr("clipsvalorant.resolve.resolve_recent", fake_resolve)
    other = web.config_for(cfg, "Someone#ELSE")
    with db.session(other.db_path) as conn:
        for n in range(3):
            conn.execute(
                "INSERT INTO match (id, started_at, started_at_epoch, "
                "ingested_at, raw_json) VALUES (?,?,?,?,?)",
                (f"m{n}", "", RECENT - n * 3600, "", "{}"))
    cfg = dataclasses.replace(cfg, twitch_client_id="id", twitch_client_secret="s")
    job = web.Job(id="j", riot_id="Someone#ELSE", key="")
    web.run_search(cfg, job, days=14, watch=False)

    snap = job.snapshot()
    assert snap["state"] == "done"
    assert snap["phase"] == "done"
    assert snap["total"] == 3 and snap["done"] == 3


def test_free_port_falls_back_when_one_is_taken():
    with ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(None)) as taken:
        port = taken.server_address[1]
        assert web.free_port(port) != port


def test_progress_callback_cannot_break_a_resolve(cfg):
    """A progress bar is never a reason to lose work that already happened."""
    from clipsvalorant.resolve import resolve_recent

    with db.session(cfg.db_path) as conn:
        conn.execute("INSERT INTO match (id, started_at, started_at_epoch, "
                     "ingested_at, raw_json) VALUES (?,?,?,?,?)",
                     ("m1", "", MATCH_START, "", "{}"))
    report = resolve_recent(cfg, match_ids=[], on_match=lambda *a: 1 / 0)
    assert report.matches_checked == 0  # no rows, no crash


def test_read_only_connection_rejects_writes(tmp_path):
    path = tmp_path / "ro.db"
    with db.session(path):
        pass
    conn = web._read_only(path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE x (y)")


# ------------------------------------------------------- the match timeline

def add_kills(conn, *, match_id="m1", rounds=(), my_puuid="me"):
    """Kills for one match. `rounds` is (round, start_ms, [(ms, mine)], died_ms)."""
    conn.execute(
        "INSERT OR REPLACE INTO match_player (match_id, puuid, name, tag, is_me) "
        "VALUES (?,?,?,?,1)", (match_id, my_puuid, "Me", "0001"),
    )
    for rnd, start_ms, kills, died_ms in rounds:
        for ms, mine in kills:
            # A kill that is not yours is somebody else's, not your death.
            # Your death is `died_ms`, and conflating the two here hid a real
            # question: which death counts when there are several.
            conn.execute(
                "INSERT INTO kill (match_id, round, time_in_match_ms, "
                "time_in_round_ms, killer_name, victim_name, victim_puuid, "
                "weapon, is_mine) VALUES (?,?,?,?,?,?,?,?,?)",
                (match_id, rnd, ms, ms - start_ms, "Me" if mine else "Them",
                 "Victim" if mine else "Bystander",
                 "" if mine else "someone-else", "Vandal", 1 if mine else 0),
            )
        if died_ms is not None:
            conn.execute(
                "INSERT INTO kill (match_id, round, time_in_match_ms, "
                "time_in_round_ms, killer_name, victim_name, victim_puuid, "
                "weapon, is_mine) VALUES (?,?,?,?,?,?,?,?,?)",
                (match_id, rnd, died_ms, died_ms - start_ms, "Them", "Me",
                 my_puuid, "Phantom", 0),
            )
    conn.commit()


def test_timeline_dates_each_round_from_the_two_clocks(conn):
    """Riot puts both clocks on every kill, so their difference is the round."""
    add_everything(conn)
    add_kills(conn, rounds=[
        (0, 55_000, [(70_000, True)], None),
        (1, 150_000, [(165_000, True)], 180_000),
    ])
    timeline = web.match_timeline(conn, "m1")
    starts = [r["start_ms"] for r in timeline["rounds"]]
    assert starts == [55_000, 150_000]
    assert timeline["last_ms"] == 180_000


def test_timeline_separates_your_kills_from_your_death(conn):
    add_everything(conn)
    add_kills(conn, rounds=[
        (0, 55_000, [(70_000, True), (75_000, False), (80_000, True)], 90_000),
    ])
    rnd = web.match_timeline(conn, "m1")["rounds"][0]
    assert [k["ms"] for k in rnd["kills"]] == [70_000, 80_000]
    assert rnd["died_ms"] == 90_000


def test_a_round_nobody_died_in_is_absent_rather_than_guessed(conn):
    """It has no kill to carry the clocks, and an interpolated timestamp is
    exactly the thing that made every link wrong the first time."""
    add_everything(conn)
    add_kills(conn, rounds=[
        (0, 55_000, [(70_000, True)], None),
        (5, 400_000, [(420_000, True)], None),
    ])
    assert [r["round"] for r in web.match_timeline(conn, "m1")["rounds"]] == [0, 5]


# ------------------------------------------------- every moment is reachable

def test_every_round_reaches_the_page_not_just_the_highlights(conn):
    """The complaint that started this: a long match showed four things."""
    add_everything(conn, at_ms=(120_000,), shift_s=100)
    add_kills(conn, rounds=[
        (r, 55_000 + r * 90_000, [(60_000 + r * 90_000, True)], None)
        for r in range(12)
    ])
    group = web.clip_groups(conn)[0]
    assert len(group["clips"]) == 1        # one highlight, as before
    assert len(group["rounds"]) == 12      # and now the whole match
    assert group["kill_count"] == 12


def test_a_moment_is_drawn_where_it_happens_and_played_before_it(conn):
    """`at` positions the marker, `offset` starts the video. Collapsing the
    two would mean every marker sat eight seconds early on the scrubber."""
    add_everything(conn, at_ms=(120_000,), shift_s=100)
    add_kills(conn, rounds=[(0, 55_000, [(120_000, True)], None)])
    clip = web.clip_groups(conn)[0]["clips"][0]
    assert clip["at"] - clip["offset"] == web.LEAD_S
    assert clip["stamp"] == calibrate.stamp(clip["offset"])


def test_a_round_starts_where_it_starts(conn):
    """No lead-in on a round, or every one would open in the previous one's
    aftermath rather than on the barriers dropping."""
    add_everything(conn, shift_s=100)
    add_kills(conn, rounds=[(3, 300_000, [(315_000, True)], None)])
    rnd = next(r for r in web.clip_groups(conn)[0]["rounds"] if r["round"] == 3)
    assert rnd["at"] == rnd["offset"]
    assert rnd["at"] == calibrate.moment_offset(
        match_start_epoch=MATCH_START, vod_created_epoch=VOD_START,
        time_in_match_ms=300_000, shift_s=100,
    )


def test_the_span_covers_the_whole_match(conn):
    """The scrubber is laid out from this, so a wrong span puts every marker
    in the wrong place even when each timestamp is right."""
    add_everything(conn, shift_s=100)
    add_kills(conn, rounds=[
        (0, 55_000, [(70_000, True)], None),
        (9, 900_000, [(950_000, True)], None),
    ])
    span = web.clip_groups(conn)[0]["span"]
    assert span["start"] < span["end"]
    assert span["start"] == web.clip_groups(conn)[0]["rounds"][0]["at"]


def test_highlights_are_ranked_best_first(conn):
    """The featured list opens with the best play, not the earliest one."""
    add_everything(conn, at_ms=())
    for rnd, kind, label, kills in [
        (1, "triple", "3K", 3), (2, "ace", "Ace", 5), (3, "clutch", "1v3", 3),
    ]:
        conn.execute(
            "INSERT INTO highlight (match_id, round, kind, label, kill_count, "
            "time_in_match_ms, end_time_in_match_ms, round_won) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("m1", rnd, kind, label, kills, rnd * 100_000, rnd * 100_000 + 5000, 1),
        )
    conn.commit()
    assert [c["kind"] for c in web.clip_groups(conn)[0]["clips"]] == [
        "ace", "clutch", "triple",
    ]


def test_a_kill_outside_the_broadcast_is_marked_not_dropped(conn):
    """A streamer who joined late is a real find, and hiding their early
    rounds looks identical to a detector that missed them."""
    add_everything(conn, vod_start=MATCH_START + 400, shift_s=100)
    add_kills(conn, rounds=[
        (0, 55_000, [(60_000, True)], None),
        (8, 800_000, [(820_000, True)], None),
    ])
    rounds = web.clip_groups(conn)[0]["rounds"]
    assert rounds[0]["playable"] is False and rounds[0]["why_not"]
    assert rounds[1]["playable"] is True


def test_group_ids_are_stable_across_reloads(conn):
    """The page keeps your selection through a refresh by matching on this."""
    add_everything(conn, channel="one")
    add_everything(conn, channel="two", match_id="m2")
    ids = [g["id"] for g in web.clip_groups(conn)]
    assert ids == [g["id"] for g in web.clip_groups(conn)]
    assert len(set(ids)) == 2
