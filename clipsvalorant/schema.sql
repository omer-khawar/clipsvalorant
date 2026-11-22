-- Phase 1 + 2 schema. See PLAN.md section 2.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS riot_account (
    puuid       TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    tag         TEXT NOT NULL,
    region      TEXT,
    is_me       INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_riot_account_name
    ON riot_account (name COLLATE NOCASE, tag COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS match (
    id                TEXT PRIMARY KEY,
    started_at        TEXT NOT NULL,          -- ISO 8601 UTC
    started_at_epoch  INTEGER NOT NULL,       -- unix seconds, for interval queries
    game_length_ms    INTEGER,
    ended_at_epoch    INTEGER,
    map               TEXT,
    mode              TEXT,
    queue             TEXT,
    region            TEXT,
    cluster           TEXT,
    my_team           TEXT,
    my_team_won       INTEGER,
    rounds_won        INTEGER,
    rounds_lost       INTEGER,
    ingested_at       TEXT NOT NULL,
    -- When the streamer search last ran over this match, and how often. A VOD can
    -- appear hours after the match and clips accumulate for days, so a young match
    -- is worth re-checking; an old one is past Twitch's retention and never is.
    last_resolved_at  TEXT,
    resolve_attempts  INTEGER NOT NULL DEFAULT 0,
    raw_json          TEXT NOT NULL           -- source of truth; schemas drift
);

CREATE INDEX IF NOT EXISTS idx_match_started ON match (started_at_epoch);

CREATE TABLE IF NOT EXISTS match_player (
    match_id    TEXT NOT NULL REFERENCES match(id) ON DELETE CASCADE,
    puuid       TEXT NOT NULL,
    name        TEXT NOT NULL,
    tag         TEXT NOT NULL,
    team        TEXT,
    agent       TEXT,
    kills       INTEGER,
    deaths      INTEGER,
    assists     INTEGER,
    score       INTEGER,
    is_me       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (match_id, puuid)
);

CREATE INDEX IF NOT EXISTS idx_match_player_name
    ON match_player (name COLLATE NOCASE, tag COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS kill (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id            TEXT NOT NULL REFERENCES match(id) ON DELETE CASCADE,
    round               INTEGER NOT NULL,
    time_in_match_ms    INTEGER NOT NULL,
    time_in_round_ms    INTEGER,
    killer_puuid        TEXT,
    killer_name         TEXT,
    victim_puuid        TEXT,
    victim_name         TEXT,
    weapon              TEXT,
    assist_count        INTEGER NOT NULL DEFAULT 0,
    is_mine             INTEGER NOT NULL DEFAULT 0,
    UNIQUE (match_id, round, time_in_match_ms, killer_puuid, victim_puuid)
);

CREATE INDEX IF NOT EXISTS idx_kill_mine ON kill (match_id, is_mine);

-- What happened in each round, which is what the timeline draws.
--
-- Derivable from `match.raw_json` on demand, and was for a while, but the
-- page asks for it once per match per request and re-parsing a 400KB payload
-- to answer "who won round 7" is work nobody needs doing twice.
CREATE TABLE IF NOT EXISTS round (
    match_id        TEXT NOT NULL REFERENCES match(id) ON DELETE CASCADE,
    round           INTEGER NOT NULL,
    result          TEXT,                 -- Elimination|Defuse|Bomb|Time
    winning_team    TEXT,
    -- Attacker or Defender. Sides swap at half time, so the team alone does
    -- not say how a round was played.
    winning_role    TEXT,
    ceremony        TEXT,
    plant_ms        INTEGER,              -- into the round, not the match
    plant_site      TEXT,
    plant_x         REAL,
    plant_y         REAL,
    defuse_ms       INTEGER,
    PRIMARY KEY (match_id, round)
);

CREATE INDEX IF NOT EXISTS idx_round_match ON round (match_id);


-- One row per notable thing you did, which is what Phase 2 turns into VOD links.
CREATE TABLE IF NOT EXISTS highlight (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id            TEXT NOT NULL REFERENCES match(id) ON DELETE CASCADE,
    round               INTEGER NOT NULL,
    kind                TEXT NOT NULL,        -- triple|quadra|ace|multi|clutch|clutch_lost
    label               TEXT NOT NULL,        -- human readable, e.g. "Ace" / "1v3 clutch"
    kill_count          INTEGER NOT NULL,     -- raw opponent kills (tracker.gg comparable)
    distinct_victims    INTEGER,              -- different opponents; lower after a rez
    time_in_match_ms    INTEGER NOT NULL,     -- first kill of the sequence
    end_time_in_match_ms INTEGER NOT NULL,    -- last kill of the sequence
    round_won           INTEGER,
    -- Riot's own round label. Corroborating only: one per round, awarded to the
    -- winning team, so an ace in a lost round carries CeremonyCloser instead.
    ceremony            TEXT,
    UNIQUE (match_id, round, kind)
);

CREATE INDEX IF NOT EXISTS idx_highlight_match ON highlight (match_id);


-- ---------------------------------------------------------------- Phase 2

CREATE TABLE IF NOT EXISTS twitch_channel (
    id              TEXT PRIMARY KEY,       -- Twitch user id
    login           TEXT NOT NULL,
    display_name    TEXT,
    description     TEXT,                   -- channel bio; mined for NAME#TAG
    -- When that bio was read from /helix/users. `last_seen` cannot stand in for
    -- it: a channel is seen again every time it turns up in a clip pool, and
    -- only /helix/users carries the bio at all.
    description_fetched_at TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_twitch_channel_login ON twitch_channel (login COLLATE NOCASE);

-- The interval index. A VOD is retroactive, so a nightly sweep loses nothing.
CREATE TABLE IF NOT EXISTS twitch_vod (
    id              TEXT PRIMARY KEY,
    channel_id      TEXT NOT NULL,
    stream_id       TEXT,                   -- joins back to a live-stream observation
    title           TEXT,
    created_at      TEXT NOT NULL,
    created_epoch   INTEGER NOT NULL,
    duration_s      INTEGER NOT NULL,
    end_epoch       INTEGER NOT NULL,
    url             TEXT,
    fetched_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_twitch_vod_window ON twitch_vod (created_epoch, end_epoch);
CREATE INDEX IF NOT EXISTS idx_twitch_vod_channel ON twitch_vod (channel_id);

-- THE asset. Never delete a confirmed row; every discovery is permanent.
CREATE TABLE IF NOT EXISTS alias (
    riot_name       TEXT NOT NULL,
    riot_tag        TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    source          TEXT NOT NULL,    -- name_match|bio_regex|title_regex|clip_ocr|vod_ocr|manual
    confidence      REAL NOT NULL DEFAULT 0.5,
    confirmed_at    TEXT NOT NULL,
    PRIMARY KEY (riot_name COLLATE NOCASE, riot_tag COLLATE NOCASE, channel_id)
);

CREATE TABLE IF NOT EXISTS encounter (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        TEXT NOT NULL REFERENCES match(id) ON DELETE CASCADE,
    puuid           TEXT NOT NULL,
    riot_id         TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    channel_login   TEXT NOT NULL,
    vod_id          TEXT,
    their_team      TEXT,
    same_team       INTEGER,
    confidence      REAL NOT NULL DEFAULT 0.5,
    verified_how    TEXT,
    -- confirmed / likely / unconfirmed / rejected. A name match plus a broadcast
    -- that merely spans the match is never better than 'likely': see resolve.py.
    verdict         TEXT,
    evidence        TEXT,
    found_at        TEXT NOT NULL,
    -- When this row was last settled by actually looking. found_at never
    -- moves, so without this a card could not tell "confirmed in this scan"
    -- from "remembered from a fortnight ago and not re-checked since".
    checked_at      TEXT,
    UNIQUE (match_id, puuid, channel_id)
);

CREATE INDEX IF NOT EXISTS idx_encounter_match ON encounter (match_id);

-- Superseded by match_vod_offset below, which this never grew a row in.
-- Keyed by VOD alone, which cannot work: one broadcast routinely contains
-- several matches, and each one needs its own answer.
CREATE TABLE IF NOT EXISTS vod_calibration (
    vod_id              TEXT PRIMARY KEY,
    measured_offset_s   REAL NOT NULL,
    drift_ms_per_hour   REAL,
    method              TEXT,
    measured_at         TEXT NOT NULL
);

-- How far a match sits into a broadcast beyond what its own timestamp says.
--
-- `metadata.started_at` is when the game server session opened - the queue
-- pop - while `time_in_match_in_ms` counts from when the map finished
-- loading. Agent select and loading sit between them, measured at 101, 109
-- and 125 seconds on three broadcasts. Adding the two clocks together lands
-- about a round and a half early, in the right match, on the right map,
-- which is why nothing caught it for so long.
--
-- `method` is 'measured' when a countdown was read off the screen and
-- 'estimated' when the median stood in, and the two are never shown alike.
CREATE TABLE IF NOT EXISTS match_vod_offset (
    match_id        TEXT NOT NULL REFERENCES match(id) ON DELETE CASCADE,
    vod_id          TEXT NOT NULL,
    shift_s         REAL NOT NULL,
    method          TEXT NOT NULL,
    votes           INTEGER NOT NULL DEFAULT 0,
    measured_at     TEXT NOT NULL,
    PRIMARY KEY (match_id, vod_id)
);
