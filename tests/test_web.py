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

import base64
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


def test_a_match_with_nothing_starred_in_it_is_still_a_find(conn):
    """Found by counting: the log said five confirmed, the page showed four.

    `highlight` used to be an inner join, so a match containing no ace, no
    clutch and no multi-kill produced no rows at all and the streamer
    vanished from the page entirely. Not "a confirmed broadcast with nothing
    starred in it" - nothing, which looks exactly like the scan having
    missed them. An ordinary forty-minute match where you took nine kills
    and no multi-kill still has every round and every one of those kills to
    watch, and that is what the timeline below the player is for.
    """
    add_everything(conn, at_ms=())
    groups = web.clip_groups(conn)
    assert len(groups) == 1
    assert groups[0]["verdict"] == "confirmed"
    assert groups[0]["clips"] == []  # nothing starred, and that is fine


def test_a_starred_match_still_lists_its_moments(conn):
    """The other half of the same join: LEFT must not cost us the moments."""
    add_everything(conn, at_ms=(1000, 2000, 3000))
    groups = web.clip_groups(conn)
    assert len(groups) == 1 and len(groups[0]["clips"]) == 3


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

    def get(self, path, client=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if client:
            req.add_header("X-Forwarded-For", client)
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")

    def post(self, path, payload, client=None):
        """A request, optionally from somebody other than this machine.

        The queue tells people apart by address, so simulating a second
        person means sending the header Fly's proxy would have set.
        """
        headers = {"Content-Type": "application/json"}
        if client:
            headers["X-Forwarded-For"] = client
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")

    def wait_started(self, n=1, timeout=3.0):
        """Block until the worker has picked up n scans."""
        end = time.time() + timeout
        while time.time() < end:
            if len(self.started) >= n:
                return True
            time.sleep(0.01)
        return False

    def close(self):
        self.release.set()
        self.state.close()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def _no_password_unless_a_test_asks_for_one(monkeypatch):
    """Whether these tests pass must not depend on the developer's own shell.

    The gate reads its password from the environment at request time, so a
    CLIPS_PASSWORD exported for a deployment would otherwise turn every
    request in this file into a 401.
    """
    monkeypatch.delenv(web.PASSWORD_ENV, raising=False)


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


def test_the_summary_carries_the_account_and_its_own_region(srv, cfg):
    """The chip shows the region of the account on screen.

    Not the server's configuration. A scan works the shard out from the
    Riot ID itself and overrides whatever this machine was set to, so a
    chip reading the server's setting told every visitor NA whoever they
    were and wherever they play.
    """
    _, data = srv.get("/api/clips")
    assert data["summary"]["region"] == cfg.region
    assert data["summary"]["riot_id"] == cfg.riot_id


def test_clips_for_an_unknown_account_explains_itself(srv):
    status, data = srv.get("/api/clips?key=nobody")
    assert status == 200 and data["groups"] == [] and data["error"]


def test_search_starts_a_job_and_passes_the_options_through(srv):
    status, job = srv.post("/api/search", {
        "riot_id": "Other#0002", "days": 3, "mode": "unrated", "watch": False,
    })
    # Queued or already picked up - which of the two depends on how fast
    # the worker thread woke, and the page handles both.
    assert status == 202 and job["state"] in ("queued", "running")
    assert job["id"]
    assert srv.wait_started(1)
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


def test_one_person_cannot_hold_two_places(srv):
    """One scan each, which is what stops an impatient refresh filling the line."""
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


def test_job_endpoint_knows_nothing_without_a_ticket(srv):
    _, data = srv.get("/api/job")
    assert data["state"] == "unknown"


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


def _store_match(conn, match_id, when):
    """A bare match row, which is all the window arithmetic reads."""
    conn.execute(
        "INSERT INTO match (id, started_at, started_at_epoch, ingested_at, raw_json)"
        " VALUES (?,?,?,?,?)",
        (match_id, "", int(when), "", "{}"),
    )


class _NoReport:
    confirmed: tuple = ()
    candidates: tuple = ()
    # The real report splits these out of `candidates`, and the progress
    # line reads them both.
    likely: tuple = ()
    unverified: tuple = ()
    errors: tuple = ()
    # Falsy, so the "we changed our mind about stored verdicts" note stays
    # out of a run that restated nothing.
    restated = None

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

    def fake_resolve(acct, *, match_ids, watch_vods, on_match, **_):
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
    monkeypatch.setattr("clipsvalorant.web._correct_region", lambda acct, job: acct)
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


def test_the_ceiling_warning_only_fires_when_the_window_was_truncated(cfg, monkeypatch):
    """A busy player can fill the listing and still have the whole window
    covered. Warning there would cry wolf on every scan they ever run, which
    is how a true warning stops being read.

    Whether it was covered is ingest's answer, not something inferred from
    the dates lying in the database: ingest is the half that walks the pages,
    so it is the only half that knows whether the history ran out or the
    ceiling did."""
    import time as _time

    from clipsvalorant.ingest import IngestReport

    monkeypatch.setattr("clipsvalorant.web._correct_region", lambda acct, job: acct)
    monkeypatch.setattr(
        "clipsvalorant.ingest.ingest",
        lambda c, **k: IngestReport(seen=40, stored=0, complete=True),
    )
    monkeypatch.setattr("clipsvalorant.resolve.resolve_recent",
                        lambda *a, **k: _NoReport())

    acct = web.config_for(cfg, "Busy#NA1")
    now = int(_time.time())
    with db.session(acct.db_path) as conn:
        for n in range(40):
            _store_match(conn, f"old-{n}", now - (20 - n // 4) * 86400)
        conn.commit()

    job = web.Job(id="j", riot_id="Busy#NA1", key="")
    web.run_search(cfg, job, days=14, fetch=40, watch=False)
    assert not any("ceiling" in note for note in job.notes), job.notes


def test_the_ceiling_warning_does_fire_when_the_window_was_truncated(cfg, monkeypatch):
    """And when it fires it says how far back the listing actually got, so
    the number in front of the user is the window they really received."""
    import time as _time

    from clipsvalorant.ingest import IngestReport

    now = int(_time.time())
    monkeypatch.setattr("clipsvalorant.web._correct_region", lambda acct, job: acct)
    monkeypatch.setattr(
        "clipsvalorant.ingest.ingest",
        lambda c, **k: IngestReport(
            seen=40, stored=0, complete=False, oldest_listed=now - 3 * 86400,
        ),
    )
    monkeypatch.setattr("clipsvalorant.resolve.resolve_recent",
                        lambda *a, **k: _NoReport())

    acct = web.config_for(cfg, "Busier#NA1")
    with db.session(acct.db_path) as conn:
        for n in range(40):
            _store_match(conn, f"new-{n}", now - 3 * 3600 * (n % 24) - 3600)
        conn.commit()

    job = web.Job(id="j", riot_id="Busier#NA1", key="")
    web.run_search(cfg, job, days=14, fetch=40, watch=False)
    note = next((n for n in job.notes if "ceiling" in n), None)
    assert note, job.notes
    assert "3.0 days back" in note
    assert "not the full 14 days" in note


# ------------------------------------------------------------ the queue

# What the queue is for: one HenrikDev key, several people. Running every
# search on arrival does not make them finish sooner - they share the same
# requests per minute and all crawl - and refusing the second one, which is
# what this did when it only ever had one user, tells somebody to come back
# with no idea when. The tests below are mostly about the thing the old
# behaviour could not express: a place in line, and who it belongs to.


def _until(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_second_person_is_queued_rather_than_refused(srv):
    srv.post("/api/search", {"riot_id": "First#0001"}, client="10.0.0.1")
    assert srv.wait_started(1)
    status, second = srv.post("/api/search", {"riot_id": "Second#0002"}, client="10.0.0.2")
    assert status == 202
    assert second["state"] == "queued"
    assert second["position"] == 1 and second["ahead"] == 0


def test_the_line_is_first_come_first_served(srv):
    srv.post("/api/search", {"riot_id": "First#0001"}, client="10.0.0.1")
    assert srv.wait_started(1)
    _, b = srv.post("/api/search", {"riot_id": "Bee#0002"}, client="10.0.0.2")
    _, c = srv.post("/api/search", {"riot_id": "Cee#0003"}, client="10.0.0.3")
    assert (b["position"], c["position"]) == (1, 2)
    assert c["ahead"] == 1 and c["eta"] > b["eta"]


def test_a_waiting_scan_is_told_what_is_running_instead(srv):
    """Otherwise a queue is indistinguishable from a page that has hung."""
    srv.post("/api/search", {"riot_id": "Loud#0001"}, client="10.0.0.1")
    assert srv.wait_started(1)
    _, waiting = srv.post("/api/search", {"riot_id": "Quiet#0002"}, client="10.0.0.2")
    assert [n["riot_id"] for n in waiting["now"]] == ["Loud#0001"]


def test_your_ticket_is_how_you_find_your_own_scan(srv):
    _, job = srv.post("/api/search", {"riot_id": "Mine#0001"}, client="10.0.0.1")
    _, mine = srv.get("/api/job?ticket=" + job["id"])
    assert mine["riot_id"] == "Mine#0001"


def test_a_ticket_that_means_nothing_is_not_an_error(srv):
    """A ticket from before a restart has to put the page back to usable."""
    _, data = srv.get("/api/job?ticket=nonsense")
    assert data["state"] == "unknown"


def test_leaving_the_queue_gives_up_the_place(srv):
    srv.post("/api/search", {"riot_id": "First#0001"}, client="10.0.0.1")
    assert srv.wait_started(1)
    _, waiting = srv.post("/api/search", {"riot_id": "Leaving#0002"}, client="10.0.0.2")
    status, after = srv.post("/api/cancel", {"ticket": waiting["id"]}, client="10.0.0.2")
    assert status == 202 and after["state"] == "cancelled"
    _, stats = srv.get("/api/queue")
    assert stats["waiting"] == 0


def test_you_cannot_cancel_somebody_elses_scan(srv):
    """The ticket is unguessable; checking who holds it is what makes that count."""
    srv.post("/api/search", {"riot_id": "First#0001"}, client="10.0.0.1")
    assert srv.wait_started(1)
    _, theirs = srv.post("/api/search", {"riot_id": "Theirs#0002"}, client="10.0.0.2")
    with pytest.raises(urllib.error.HTTPError) as exc:
        srv.post("/api/cancel", {"ticket": theirs["id"]}, client="10.0.0.9")
    assert exc.value.code == 403


def test_cancelling_nothing_says_so(srv):
    with pytest.raises(urllib.error.HTTPError) as exc:
        srv.post("/api/cancel", {"ticket": "never-existed"})
    assert exc.value.code == 404


def test_the_queue_endpoint_needs_no_ticket(srv):
    """The page shows the state of the line to everyone, scanning or not."""
    srv.post("/api/search", {"riot_id": "Live#0001"}, client="10.0.0.1")
    assert srv.wait_started(1)
    _, stats = srv.get("/api/queue")
    assert [r["riot_id"] for r in stats["running"]] == ["Live#0001"]
    assert stats["waiting"] == 0 and stats["workers"] == 1


def test_the_line_has_an_end(srv, monkeypatch):
    monkeypatch.setattr(web, "MAX_WAITING", 2)
    srv.post("/api/search", {"riot_id": "Run#0001"}, client="10.0.0.1")
    assert srv.wait_started(1)
    srv.post("/api/search", {"riot_id": "Aay#0002"}, client="10.0.0.2")
    srv.post("/api/search", {"riot_id": "Bee#0003"}, client="10.0.0.3")
    with pytest.raises(urllib.error.HTTPError) as exc:
        srv.post("/api/search", {"riot_id": "Cee#0004"}, client="10.0.0.4")
    assert exc.value.code == 409


def test_the_next_scan_starts_when_the_one_ahead_finishes(cfg):
    """The whole point of a queue, and the one part a handler cannot show."""
    ran = []
    hold = threading.Event()

    def runner(_cfg, job, **kwargs):
        ran.append(job.riot_id)
        if len(ran) == 1:
            hold.wait(timeout=3)
        job.state = "done"
        job.finished = time.time()

    queue = web.Queue(cfg, runner)
    try:
        queue.submit("First#0001", client="a")
        second = queue.submit("Second#0002", client="b")
        assert _until(lambda: ran == ["First#0001"])
        assert queue.snapshot(second.id)["state"] == "queued"
        hold.set()
        assert _until(lambda: ran == ["First#0001", "Second#0002"])
        assert _until(lambda: queue.snapshot(second.id)["state"] == "done")
        assert queue.stats()["waiting"] == 0
    finally:
        hold.set()
        queue.close()


def test_a_scan_cancelled_while_it_waited_never_runs(cfg):
    ran = []
    hold = threading.Event()

    def runner(_cfg, job, **kwargs):
        ran.append(job.riot_id)
        if len(ran) == 1:
            hold.wait(timeout=3)
        job.state = "done"
        job.finished = time.time()

    queue = web.Queue(cfg, runner)
    try:
        queue.submit("First#0001", client="a")
        skipped = queue.submit("Skipped#0002", client="b")
        assert _until(lambda: ran == ["First#0001"])
        queue.cancel(skipped.id, client="b")
        hold.set()
        assert _until(lambda: queue.stats()["waiting"] == 0)
        assert "Skipped#0002" not in ran
    finally:
        hold.set()
        queue.close()


def test_a_worker_that_blows_up_still_frees_the_line(cfg):
    """A dead worker would leave everyone behind it waiting forever."""
    def runner(_cfg, job, **kwargs):
        if job.riot_id.startswith("Doomed"):
            raise RuntimeError("the runner fell over")
        job.state = "done"
        job.finished = time.time()

    queue = web.Queue(cfg, runner)
    try:
        job = queue.submit("Doomed#0001", client="a")
        assert _until(lambda: queue.snapshot(job.id)["state"] == "error")
        assert "the runner fell over" in queue.snapshot(job.id)["error"]
        later = queue.submit("After#0002", client="b")
        assert _until(lambda: queue.snapshot(later.id)["state"] == "done")
    finally:
        queue.close()


# ------------------------------------------------------------ the password

# What the gate is for: bound to localhost the page needs no authentication,
# because nothing else is on the interface. Deployed, the same page hands
# anyone who finds the URL a HenrikDev key to spend and the one search slot
# the whole server has. So the test that matters most here is that POST
# /api/search - the only route that costs anything - is shut.


def _raw_get(port, path, auth=None):
    """Status, body and headers for one request, 401s included.

    urllib raises on a 4xx, which is the case under test rather than a
    failure, so both paths come back the same shape.
    """
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if auth is not None:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _basic(password, user="anyone"):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_no_password_configured_leaves_the_page_open(srv):
    """The localhost default stays as it was: nobody types a password to run this."""
    status, body, _ = _raw_get(srv.port, "/")
    assert status == 200 and body.startswith(b"<!doctype html>")


@pytest.mark.parametrize("path", ["/", "/api/state", "/api/clips", "/api/job"])
def test_a_password_gates_every_route(srv, monkeypatch, path):
    monkeypatch.setenv(web.PASSWORD_ENV, "hunter2")
    status, _, headers = _raw_get(srv.port, path)
    assert status == 401
    # Without the challenge the browser never offers to collect one.
    assert "Basic" in headers.get("WWW-Authenticate", "")


def test_the_password_opens_it(srv, monkeypatch):
    monkeypatch.setenv(web.PASSWORD_ENV, "hunter2")
    status, body, _ = _raw_get(srv.port, "/", _basic("hunter2"))
    assert status == 200 and body.startswith(b"<!doctype html>")


def test_any_username_works(srv, monkeypatch):
    """One field is less to mistype on a phone, and there are no accounts to tell apart."""
    monkeypatch.setenv(web.PASSWORD_ENV, "hunter2")
    for user in ("", "omer", "root"):
        status, _, _ = _raw_get(srv.port, "/api/state", _basic("hunter2", user))
        assert status == 200, user


def test_the_wrong_password_is_refused(srv, monkeypatch):
    monkeypatch.setenv(web.PASSWORD_ENV, "hunter2")
    status, _, _ = _raw_get(srv.port, "/", _basic("hunter3"))
    assert status == 401


@pytest.mark.parametrize("header", [
    "",
    "Bearer hunter2",
    "Basic",
    "Basic !!!not-base64!!!",
    "Basic " + base64.b64encode(b"no-colon-in-here").decode(),
    "Basic " + base64.b64encode(b"\xff\xfe").decode(),
])
def test_a_header_that_is_not_a_password_is_refused(srv, monkeypatch, header):
    """Every way of being malformed has to end in a 401, not a traceback."""
    monkeypatch.setenv(web.PASSWORD_ENV, "hunter2")
    status, _, _ = _raw_get(srv.port, "/", header)
    assert status == 401


def test_searching_is_gated_too(srv, monkeypatch):
    """The expensive route. An open POST is an open API key."""
    monkeypatch.setenv(web.PASSWORD_ENV, "hunter2")
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}/api/search",
        data=json.dumps({"riot_id": "Other#0002"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req)
    assert caught.value.code == 401
    assert srv.started == []


def test_the_favicon_answers_without_the_password(srv, monkeypatch):
    """A browser asks for the tab icon before anyone can have typed one."""
    monkeypatch.setenv(web.PASSWORD_ENV, "hunter2")
    status, body, headers = _raw_get(srv.port, "/favicon.svg")
    assert status == 200
    assert headers.get("Content-Type") == "image/svg+xml"
    assert body.startswith(b"<svg") and b"</svg>" in body


def test_the_health_check_answers_without_the_password(srv, monkeypatch):
    """A 401 here reads as a dead machine, and gets it restarted under a running scan."""
    monkeypatch.setenv(web.PASSWORD_ENV, "hunter2")
    status, body, _ = _raw_get(srv.port, "/healthz", None)
    assert status == 200 and body.strip() == b"ok"


def test_how_long_rounds_to_words_that_are_true():
    """The estimate behind it is an average over a handful of matches, so
    quoting seconds would claim a precision the number does not have."""
    assert web._how_long(0) == "1s"
    assert web._how_long(45) == "45s"
    assert web._how_long(89) == "89s"
    assert web._how_long(600) == "10 min"
    assert web._how_long(3599) == "1h"
    assert web._how_long(3600) == "1h"
    assert web._how_long(4500) == "1h 15m"


def test_the_progress_line_estimates_from_this_machines_own_pace(cfg, monkeypatch):
    """A match costs about 4.7s on a laptop and about 34s on the 1/8 OCPU
    box, so a constant would be wrong on one of them. The figure shown is
    the one this run is setting - and it is withheld until three matches
    have gone by, and on the last one, where there is nothing left to wait
    for."""
    from clipsvalorant.ingest import IngestReport

    monkeypatch.setattr("clipsvalorant.ingest.ingest",
                        lambda c, **k: IngestReport(seen=6, stored=6, complete=True))

    def fake_resolve(acct, *, match_ids, watch_vods, on_match, **_):
        for mid in match_ids:
            on_match(mid, _NoReport())
        return _NoReport()

    monkeypatch.setattr("clipsvalorant.resolve.resolve_recent", fake_resolve)
    other = web.config_for(cfg, "Pacey#NA1")
    with db.session(other.db_path) as conn:
        for n in range(6):
            _store_match(conn, f"m{n}", RECENT - n * 3600)
        conn.commit()
    cfg = dataclasses.replace(cfg, twitch_client_id="id", twitch_client_secret="s")
    monkeypatch.setattr("clipsvalorant.web._correct_region", lambda acct, job: acct)
    job = web.Job(id="j", riot_id="Pacey#NA1", key="")
    web.run_search(cfg, job, days=14, watch=False)

    checked = [ln for ln in job.snapshot()["lines"] if " checked - " in ln]
    assert len(checked) == 6
    assert "left" not in checked[0] and "left" not in checked[1]
    assert "left" in checked[2]
    assert "left" not in checked[5]


def test_the_scan_says_which_queues_it_never_looked_at(cfg, monkeypatch):
    """The complaint this answers: a scan that reports on 45 matches while
    24 more sit in queues it never asked for has not told you it stopped
    short, and a match played in Swiftplay is indistinguishable from a
    scanner that found nobody."""
    from collections import Counter

    from clipsvalorant.ingest import IngestReport

    monkeypatch.setattr("clipsvalorant.ingest.ingest",
                        lambda c, **k: IngestReport(seen=2, stored=0, complete=True))
    monkeypatch.setattr(
        "clipsvalorant.ingest.other_modes",
        lambda c, **k: Counter({"Swiftplay": 8, "Deathmatch": 8, "Custom Game": 4}),
    )
    monkeypatch.setattr("clipsvalorant.resolve.resolve_recent",
                        lambda *a, **k: _NoReport())
    monkeypatch.setattr("clipsvalorant.web._correct_region", lambda acct, job: acct)

    acct = web.config_for(cfg, "Queues#NA1")
    with db.session(acct.db_path) as conn:
        _store_match(conn, "m0", RECENT)
        conn.commit()
    cfg = dataclasses.replace(cfg, twitch_client_id="id", twitch_client_secret="s")
    job = web.Job(id="j", riot_id="Queues#NA1", key="")
    web.run_search(cfg, job, days=14, watch=False)

    lines = " ".join(job.snapshot()["lines"])
    assert "20 other match(es)" in lines
    assert "Swiftplay 8" in lines and "Custom Game 4" in lines


def test_counting_the_other_queues_can_fail_without_failing_the_scan(cfg, monkeypatch):
    """It is a footnote bought with one extra API call. A footnote must not
    be able to take the scan down with it."""
    from clipsvalorant.ingest import IngestReport

    def explode(*a, **k):
        raise RuntimeError("listing unavailable")

    monkeypatch.setattr("clipsvalorant.ingest.ingest",
                        lambda c, **k: IngestReport(seen=1, stored=0, complete=True))
    monkeypatch.setattr("clipsvalorant.ingest.other_modes", explode)
    monkeypatch.setattr("clipsvalorant.resolve.resolve_recent",
                        lambda *a, **k: _NoReport())
    monkeypatch.setattr("clipsvalorant.web._correct_region", lambda acct, job: acct)

    acct = web.config_for(cfg, "Footnote#NA1")
    with db.session(acct.db_path) as conn:
        _store_match(conn, "m0", RECENT)
        conn.commit()
    cfg = dataclasses.replace(cfg, twitch_client_id="id", twitch_client_secret="s")
    job = web.Job(id="j", riot_id="Footnote#NA1", key="")
    web.run_search(cfg, job, days=14, watch=False)

    assert job.state == "done"


def test_the_queue_control_reaches_the_api():
    """The backend has always understood mode=any. Nothing ever sent it, so
    every scan anyone ever ran was Competitive-only without saying so."""
    assert 'id="queue"' in web.PAGE
    assert 'data-mode="competitive" aria-checked="true"' in web.PAGE
    assert 'data-mode="any"' in web.PAGE
    assert "mode: S.mode" in web.PAGE
    assert 'mode: "competitive"' in web.PAGE


def test_a_second_search_does_not_pay_for_what_the_first_one_settled(cfg, monkeypatch):
    """Reported as a 30-minute scan of an account scanned several times before.

    `sweep.matches_due` has always known which matches are worth resolving;
    the page did not use it, so pressing Search re-ran the whole funnel over
    every match in the window - a name search per player and a broadcast
    listing per candidate - to arrive back at the answer already on disk.
    """
    from clipsvalorant.ingest import IngestReport

    monkeypatch.setattr("clipsvalorant.ingest.ingest",
                        lambda c, **k: IngestReport(seen=2, stored=0))
    asked: list[list[str]] = []

    def fake_resolve(acct, *, match_ids, **_):
        asked.append(list(match_ids))
        return _NoReport()

    monkeypatch.setattr("clipsvalorant.resolve.resolve_recent", fake_resolve)
    monkeypatch.setattr("clipsvalorant.web._correct_region", lambda acct, job: acct)
    other = web.config_for(cfg, "Someone#ELSE")
    with db.session(other.db_path) as conn:
        for n in range(3):
            conn.execute(
                "INSERT INTO match (id, started_at, started_at_epoch, "
                "ingested_at, raw_json, last_resolved_at, resolve_attempts) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"m{n}", "", RECENT - n * 3600, "", "{}", db.utcnow(), 1))
    cfg = dataclasses.replace(cfg, twitch_client_id="id", twitch_client_secret="s")
    job = web.Job(id="j", riot_id="Someone#ELSE", key="")
    web.run_search(cfg, job, days=14, watch=False)

    assert job.state == "done"
    assert asked == [], "re-resolved matches that were settled hours ago"
    assert "checked already" in " ".join(job.snapshot()["lines"])


# ------------------------------------- which of these did this scan look at

def test_a_remembered_find_says_it_was_not_re_checked():
    """The contradiction this removes: the page shows every streamer the
    database holds while the log counts only what this run looked at, so a
    find from a fortnight ago appeared under a log reading "0 confirmed" and
    looked exactly like a scanner that had lost one."""
    from datetime import datetime, timedelta, timezone

    from clipsvalorant.web import _is_stale, _last_looked

    now = datetime.now(timezone.utc)
    stamp = lambda dt: dt.isoformat().replace("+00:00", "Z")

    assert _last_looked(stamp(now), None) == "checked just now"
    assert not _is_stale(stamp(now))

    old = stamp(now - timedelta(days=9))
    assert _last_looked(old, None) == "not re-checked for 9 days"
    assert _is_stale(old)


def test_a_row_from_before_we_recorded_checks_does_not_invent_one():
    """Saying "not re-checked since" of a row that never had the column is
    inventing a fact. It says when it was found and stops there."""
    from datetime import datetime, timedelta, timezone

    from clipsvalorant.web import _is_stale, _last_looked

    found = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    text = _last_looked(None, found)
    assert text.startswith("found ")
    assert "re-checked" not in text
    assert _is_stale(None)


def test_restating_a_verdict_is_not_a_check(tmp_path):
    """restate_verdicts re-judges evidence already on the row. Nothing was
    looked at, so a card must not come away claiming a frame was read."""
    from clipsvalorant import db
    from clipsvalorant.resolve import restate_verdicts

    path = tmp_path / "restate.db"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO match (id, started_at, started_at_epoch, region, "
            "ingested_at, raw_json) VALUES ('m1', '2026-11-01T00:00:00Z', 1, "
            "'eu', '2026-11-01T00:00:00Z', '{}')"
        )
        conn.execute(
            "INSERT INTO encounter (match_id, puuid, riot_id, channel_id, "
            "channel_login, vod_id, confidence, verdict, evidence, found_at, "
            "checked_at) VALUES ('m1','p','NeoN#Rage','c','neonquin','v',0.95,"
            "'confirmed','{\"clips_inside_match\": 1}','2026-11-01T00:00:00Z',"
            "'2026-11-01T00:00:00Z')"
        )
        conn.commit()
        restate_verdicts(conn)
        row = conn.execute("SELECT verdict, checked_at FROM encounter").fetchone()

    assert row["verdict"] != "confirmed", "a clip alone no longer confirms"
    assert row["checked_at"] == "2026-11-01T00:00:00Z", "nothing was looked at"
