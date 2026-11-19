"""Which matches a scheduled run should bother with.

The sweep exists because of a measured fact, not a preference: across 44
matches, 1,775 of 1,783 rejections were "no broadcast of this match survived".
Twitch deletes past broadcasts after 7 to 14 days, so the deciding factor in
whether a streamer is ever found is simply *when* you look. Running often is
the feature; the identity funnel is only the mechanism.

That makes the due-list the most consequential piece of logic here. Look too
rarely and the VODs are gone; look at everything every time and you burn the
API quota on matches that were settled days ago.
"""

import time

import pytest

from clipsvalorant import db
from clipsvalorant.sweep import (
    MAX_ATTEMPTS,
    RECHECK_DAYS,
    RECHECK_INTERVAL_H,
    RETENTION_DAYS,
    matches_due,
)

NOW = 1_800_000_000


@pytest.fixture()
def conn(tmp_path):
    with db.session(tmp_path / "t.db") as c:
        yield c


def add_match(conn, mid, *, age_days=0.0, resolved_h_ago=None, attempts=0,
              highlights=1, confirmed=False):
    started = NOW - int(age_days * 86400)
    last = None
    if resolved_h_ago is not None:
        last = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                             time.gmtime(NOW - int(resolved_h_ago * 3600)))
    conn.execute(
        "INSERT INTO match (id, started_at, started_at_epoch, ingested_at, raw_json, "
        "last_resolved_at, resolve_attempts) VALUES (?,?,?,?,?,?,?)",
        (mid, "", started, "", "{}", last, attempts),
    )
    for i in range(highlights):
        conn.execute(
            "INSERT INTO highlight (match_id, round, kind, label, kill_count, "
            "time_in_match_ms, end_time_in_match_ms) VALUES (?,?,?,?,?,?,?)",
            (mid, i, "triple", "3K", 3, 1000, 2000),
        )
    if confirmed:
        conn.execute(
            "INSERT INTO encounter (match_id, puuid, riot_id, channel_id, "
            "channel_login, verdict, found_at) VALUES (?,?,?,?,?,?,?)",
            (mid, "p", "A#1", "c", "chan", "confirmed", ""),
        )
    conn.commit()


def due_ids(conn):
    ids, _, _ = matches_due(conn, now=NOW)
    return ids


def test_a_match_never_looked_at_is_always_due(conn):
    add_match(conn, "fresh", age_days=0.1)
    assert due_ids(conn) == ["fresh"]


def test_a_match_past_twitch_retention_is_never_due(conn):
    """Nothing survives out there to be found, and asking costs a call per player."""
    add_match(conn, "old", age_days=RETENTION_DAYS + 1)
    ids, expired, _ = matches_due(conn, now=NOW)
    assert ids == [] and expired == 1


def test_a_confirmed_match_is_not_looked_at_again(conn):
    add_match(conn, "done", age_days=1, resolved_h_ago=48, confirmed=True)
    assert due_ids(conn) == []


def test_a_match_with_no_highlights_of_your_own_is_skipped(conn):
    """Finding a streamer in that lobby would produce nothing to link to."""
    add_match(conn, "dull", age_days=0.1, highlights=0)
    assert due_ids(conn) == []


def test_a_young_match_is_rechecked_after_the_interval(conn):
    """A VOD appears when the broadcast ends, hours after the match, and clips
    accumulate for days. The evidence arrives after the first look."""
    add_match(conn, "ripening", age_days=1, resolved_h_ago=RECHECK_INTERVAL_H + 1)
    assert due_ids(conn) == ["ripening"]


def test_a_young_match_looked_at_recently_is_left_alone(conn):
    add_match(conn, "justlooked", age_days=1, resolved_h_ago=1)
    assert due_ids(conn) == []


def test_rechecking_stops_once_the_evidence_has_stopped_arriving(conn):
    add_match(conn, "stale", age_days=RECHECK_DAYS + 1, resolved_h_ago=100)
    assert due_ids(conn) == []


def test_rechecking_stops_after_enough_attempts(conn):
    add_match(conn, "tried", age_days=1, resolved_h_ago=100, attempts=MAX_ATTEMPTS)
    assert due_ids(conn) == []


def test_the_counts_add_up_to_every_match_considered(conn):
    add_match(conn, "a", age_days=0.1)
    add_match(conn, "b", age_days=RETENTION_DAYS + 2)
    add_match(conn, "c", age_days=1, resolved_h_ago=1)
    ids, expired, settled = matches_due(conn, now=NOW)
    assert len(ids) + expired + settled == 3
