# clipsvalorant — end-to-end build plan

A personal, free replacement for [klips.live](https://klips.live/). Enter a Riot ID, scan
match history, find players in those matches who were streaming, and surface timestamped
Twitch VOD links to your own plays from their point of view.

Reference product for scope: klips.live charges $6.99/mo for a 60-day lookback, automatic
checks on each new match, and notifications. Its free tier is a 14-day lookback and one
unlock per 7 days. We are building the paid feature set for one user.

---

## 0. The one hard problem

Everything in this project is a solved API call except a single question:

> Given `PlayerOne#NA1` from my match roster, which Twitch channel is that, when the
> channel is not called "PlayerOne"?

klips.live advertises that **72% of the streamers it finds do not use their in-game name on
Twitch**. That number is the whole product. Its own refund policy — an unlock is refunded
if it cannot confirm the streamer from the saved video — tells you they fail sometimes too.

So the design is a **funnel**: cheap layers first, expensive content-verification last, and
**every confirmation is written back to a permanent alias table** so the expensive layers
run less often over time. Recall starts below klips.live's and converges upward.

| Layer | Mechanism | Cost | Catches |
|---|---|---|---|
| 1 | Direct name match | ~10 API calls | the easy ~28% |
| 2 | Alias table lookup | 1 local query | everything learned so far |
| 3 | Self-declared harvest (bio/title regex) | offline sweep | feeds layer 2 |
| 4 | Clip-derived candidates | ~2 API calls | streamers with an audience |
| 5 | VOD index + frame fingerprint | OCR per candidate | the rest of the 72% |

---

## 1. Data sources (all confirmed)

### Valorant match data — HenrikDev unofficial API
Riot's official API is closed to us: no personal keys are issued for Valorant, and
production keys need a working app plus ToS and privacy policy. Not a path.

Keys come from the HenrikDev dashboard after joining their Discord. Basic is 30 req/min
(instant), Enhanced 90 req/min (1–2 week wait), paid tiers $10.99 / $15.99 / $25.99 a month
for 130 / 200 / 300 req/min. Rate limiting counts your call as 1 point **plus** 1 per
background Riot request it triggers; cached/stored data costs only the 1.

    GET /valorant/v1/stored-matches/{affinity}/{name}/{tag}?mode=competitive&size=20&page=1
    GET /valorant/v4/match/{affinity}/{match_id}

The second call is what makes this project possible. Confirmed from their live OpenAPI spec
(their docs site does not publish it):

    metadata.started_at          ISO 8601 date-time   <- match start, wall clock
    metadata.game_length_in_ms   int64
    metadata.map, queue, season, region, cluster
    players[]                    puuid, name, tag, team, agent, stats
    kills[]                      <- TOP-LEVEL array, not nested per round
      time_in_match_in_ms        int64  <- the number everything hangs on
      time_in_round_in_ms        int64
      round                      int32
      killer                     { puuid, name, tag, team }
      victim                     { puuid, name, tag, team }
      assistants[], weapon, location, player_locations

Caveat: stored-matches is an **accumulating subset populated on request**, not a mirror.
Matches nobody ever requested are absent. Ours will be there once we start pulling.

Their terms bar "big analytic projects" and require user consent. A personal tool on your
own data is within the spirit; worth a ticket in their Discord before heavy use.

**Fallback / supplement — local Riot client API.** Read the lockfile at
`%LOCALAPPDATA%\Riot Games\Riot Client\Config\lockfile`, get an entitlements token, call
`pd.{shard}.a.pvp.net/match-details/v1/matches/{matchID}`. Free, unthrottled, own data only,
needs the client running, undocumented. Equivalent fields are `matchInfo.gameStartMillis`,
`gameLengthMillis`, `players[].subject/gameName/tagLine/teamId`, and per-kill `gameTime` /
`roundTime` (both ms) under `roundResults[].playerStats[].kills[]`.

### Twitch — official Helix API
Base `https://api.twitch.tv/helix`, `Authorization: Bearer` + `Client-Id`. App access token
is enough for everything we need. Token bucket per client ID; read `Ratelimit-Limit` /
`Ratelimit-Remaining` / `Ratelimit-Reset` at runtime rather than trusting any documented
number.

    GET /helix/search/categories?query=VALORANT     -> the game_id, once
    GET /helix/streams?game_id=&first=100           -> live now, paginated (roster building)
    GET /helix/search/channels?query=              -> matches login + display name
    GET /helix/users?login=|id=                     -> description (channel bio), <=100/call
    GET /helix/videos?user_id=&type=archive&first=  -> past broadcasts
    GET /helix/clips?game_id=&started_at=&ended_at= -> clips in a time window

Notes that matter:
- `GET /helix/videos` takes exactly one of `id` / `user_id` / `game_id`, **has no date-range
  filter** (`period` is only all/day/week/month), and returns `duration` as a **string**
  like `"3m21s"`. Archive videos carry `stream_id`, which joins back to `GET /helix/streams`.
- `GET /helix/clips` **does** take `started_at` / `ended_at` (RFC3339, truncated to the
  minute, `ended_at` defaults to +1 week) and returns `vod_offset` (seconds into the VOD) and
  `video_id`. Pagination caps around 1,000 results.
- `created_at` on a video is documented only as "when the video was created". Community
  reports put it within seconds of stream start. **Verify empirically, do not trust.**

### What does NOT work (checked, so we don't revisit it)
- **Clipping someone else's VOD.** `POST /helix/videos/clips` requires the broadcaster's own
  or an editor's token with `editor:manage:clips` / `channel:manage:clips`, and `editor_id`
  must match the token's user. `POST /helix/clips` is live-only. Both dead ends. klips.live
  confirms the workaround: it "does not download or store the streams it finds" — it embeds
  with a start offset.
- **Historical "who was live".** No Helix endpoint. `GET /helix/streams` and
  `search/channels` are live-only. Streams Charts sells it (Twitch back to June 2019,
  1-minute granularity) if we ever want to buy our way out.
- **tracker.gg.** `/developers` is 404 — no public API. Valorant profiles are **opt-in
  private**: `tracker.gg/valorant/profile/riot/tarik%23NA1` returns "TARIK#NA1'S PROFILE IS
  PRIVATE". The Twitch button is **self-linked on TRN**, not from the Riot account (TRN staff
  confirmed). No streamer directory. `api.tracker.gg` 403s automated clients. Usable only as
  a thin browser-scraped enrichment source, never primary.
- **A Riot <-> Twitch account mapping.** The Drops link exists but is exposed by no API.

---

## 2. Data model

    riot_account(id, name, tag, puuid, region)            -- us, and everyone we meet
    match(id, started_at, ended_at, map, mode, queue, region, raw_json)
    match_player(match_id, puuid, name, tag, team, agent, is_me)
    kill(match_id, round, time_in_match_ms, killer_puuid, victim_puuid, weapon,
         assist_count, is_mine, kill_type)                -- kill_type: ace/4k/3k/clutch/1v1

    twitch_channel(id, login, display_name, description, first_seen, last_seen,
                   follower_count, streams_valorant)      -- the roster
    twitch_vod(id, channel_id, stream_id, created_at, duration_s, title)
                                                          -- the interval index
    twitch_title_observation(channel_id, observed_at, title)

    alias(riot_name, riot_tag, twitch_channel_id, source, confidence, confirmed_at)
          -- source: name_match | bio_regex | title_regex | clip_ocr | vod_ocr | manual
          -- THE asset. Never delete a confirmed row.

    encounter(match_id, twitch_channel_id, vod_id, their_team, confidence, verified_how)
    clip(encounter_id, kill_id, vod_offset_s, kill_type, label, url)

    vod_calibration(vod_id, measured_offset_s, drift_ms_per_hour, method, measured_at)

SQLite is sufficient. One file, one machine. Index `twitch_vod(created_at)` and
`alias(riot_name, riot_tag)`.

---

## 3. Components

### C1 — Match ingest
Nightly, plus on demand. `stored-matches` for your Riot ID, page until you hit matches you
already have, then `v4/match` for each new ID. Store raw JSON (schemas change; keep the
source of truth). Derive `kill` rows, tagging `is_mine` where `killer.puuid` is yours, and
classify `kill_type` by grouping your kills per round: 5 = ace, 4 = 4k, 3 = 3k; last-alive
with 2+ = clutch.

**Cost:** 1 + N calls per run. Trivial.

### C2 — Channel roster
Sweep `GET /helix/streams?game_id=<VALORANT>&first=100` and paginate. Valorant averages
**~2,800 concurrent channels**, peaking around 4,400 — so ~28–45 calls per sweep. Run it
every 30 minutes; it is **not** time-critical (see C3 for why) and exists only to accumulate
the set of channels that play Valorant. Upsert `twitch_channel`, and append every observed
title to `twitch_title_observation` — those titles are free alias fuel.

Enrich with `GET /helix/users` in batches of 100 to capture `description`.

**Cost:** ~45 calls per sweep x 48 = ~2,200/day. Nothing.

### C3 — VOD interval index
This is the piece that replaces any need for historical presence polling. **A VOD is
retroactive**: one created today describes a stream that already happened, so a nightly
sweep loses nothing. For each roster channel, `GET /helix/videos?user_id=&type=archive&
first=20`, parse the `"3m21s"` duration into seconds, store `created_at` and
`created_at + duration` as an interval.

**Cost:** 1 call per channel per night. A 20k-channel roster is ~20k calls, about 25 minutes
of sustained requests against the bucket. Fine overnight. Prune channels not seen in 90 days.

Resolving "who was broadcasting during my match" is then a **local interval query**, zero
API calls, fully backfillable up to Twitch's VOD retention (~7–14 days depending on channel
status — confirm current policy; it is the real ceiling on lookback).

### C4 — Identity resolution funnel
Layers 1–4 **generate candidates**. They do not confirm anything. Confirmation is a separate
step with its own rule, below, and it is the part that matters.

**Layer 1 — direct name match.** For each of the 9 others: `GET /helix/search/channels?
query=<name>`, plus normalized comparison (lowercase, strip spaces/underscores/dots) against
`login` and `display_name`. The zero-infrastructure path. It also collides constantly: Riot
IDs are 3–16 characters and every short one is somebody's Twitch channel already.

**Layer 2 — alias lookup.** Hash join on `(riot_name, riot_tag)`. Instant. Starts empty.
Only ever written by a *confirmed* resolution, because a wrong alias is permanent — this
layer answers first and skips every check that would have caught it.

**Layer 3 — self-declared harvest.** Regex `([A-Za-z0-9 _.-]{3,16})#([A-Za-z0-9]{3,5})`
across `twitch_channel.description` and VOD titles. A 3–5 character tag requirement is what
keeps "FINDING #1 HUSH" and "clip #42" out. Names may contain spaces and sit in the middle of
prose, so keep every plausible tail of the capture and compare on the **tagline first**: an
advertised ID whose tag differs from the player's is somebody else's account. Lots of Valorant
streamers put their tag in their bio. Channel **panels** often say "IGN: X#Y" but are **not in
Helix** — only Twitch's private GraphQL, and many are images. Nice-to-have, not a dependency.

**Layer 4 — clip-derived candidates.** The cheap precise trick:

    GET /helix/clips?game_id=<VALORANT>&started_at=<match_start>&ended_at=<match_end>

Returns clips from *any* Valorant channel in your match window, each with `broadcaster_id`,
`video_id` and `vod_offset`. Two calls and you have a candidate list biased toward streamers
with an audience — exactly who you care about. This is the layer that found an enemy streamer
whose Twitch name (`vaerie`) differs from their Riot ID (`vae#100`).

#### Confirmation — the rule that stops false positives
A broadcast that spans the match proves only that the channel was **broadcasting**. It says
nothing about what was on screen, and a channel that streams daily satisfies it for any match
ever played. That is how a lobby-mate called `Curry#2622` got matched to `twitch.tv/curry`, a
radiant Valorant streamer, whose covering broadcast was titled *SECRET DEADLOCK STREAM*.

So a candidate is `confirmed` only with a signal tying the channel to **this** match:

- **a Valorant clip cut from the covering VOD at an offset inside the match.** Clips carry
  `video_id`, `vod_offset` and `game_id`, which is the only way to reconstruct what a past
  broadcast contained — `GET /helix/videos` carries no game id at all, and `GET /helix/streams`
  only answers for channels live right now.
- **a Riot ID the channel itself advertises** (Layer 3), name and tagline both agreeing.

And ruled out when:

- clips exist for that broadcast and **not one is Valorant** — they were playing something else;
- every Riot ID the channel advertises has a different tagline;
- no broadcast holds at least 60% of the match.

Everything else — including an exact name match plus a broadcast spanning the match — is
reported as **unverified**, under a heading that says nothing proves they were in the game, and
is never written to the alias table. The VOD title is always printed: it is free, and it is
what would have caught the collision above.

A VOD is chosen by **seconds of overlap**, not pass/fail coverage. A stream picked up two
minutes into a match still holds every later round; the link maths refuses to point at a
moment before the stream began rather than clamping to `t=0`.

**Layer 5 — VOD fingerprint.** The fallback that closes the rest of the 72%, and the only
thing that can confirm a channel nobody clipped.
1. Local interval query -> VODs overlapping the match. Expect ~200–400 for a 35-minute window
   against a large roster. Pre-filter by `follower_count` / observed viewer counts to drop the
   long tail, and by `broadcaster_language` as a region proxy.
2. For each candidate, compute the predicted offset (section 4) and pull ~2 seconds with
   `yt-dlp --download-sections "*HH:MM:SS-HH:MM:SS"` — segment-only, so it is fast.
3. **Fingerprint on the round score and round timer.** You know from `kills[]` and the round
   timeline exactly what the scoreboard should read at that instant. Score progression across
   a match is near-unique. Three sampled frames make a false positive very unlikely.
4. Survivors: sample a frame ~1s after a known kill and **OCR the killfeed** for the actual
   killer/victim names. That is direct, unambiguous name confirmation.
5. Write `alias(source='vod_ocr', confidence=high)`. Never OCR that channel again.

Prior art to lean on rather than building from zero:
[valoscribe](https://github.com/naheedroomy/valoscribe) (VOD analysis, OCR + template
matching for score/timer/spike, killfeed parser, HUD coords in editable JSON configs,
Python 3.10+ / Tesseract);
[valorant-ocr-benchmark](https://github.com/jflavigne/valorant-ocr-benchmark) (killfeed
killer/victim name parsing via EasyOCR);
[valorant-hud-vision](https://github.com/HazemMamdouh/valorant-hud-vision) (classical CV,
template matching on real HUD glyphs, 2.58 ms/frame);
[gigi](https://github.com/hoangvu12/gigi) (defines ROIs as **fractions of the window** so
they scale across resolutions — copy that idea, streamer VODs vary in resolution and
bitrate).

**Expectation:** HUD OCR on a 720p low-bitrate VOD is the weakest link in the whole system.
Build the score fingerprint first and measure precision/recall on matches you can verify by
hand before trusting it.

### C5 — Timestamp resolution

    offset_s = (match.started_at + kill.time_in_match_ms/1000) - vod.created_at

Then correct it, because the raw number will be wrong by some amount:
- `created_at` is not contractually the VOD's second 0.
- `metadata.started_at` semantics (end of agent select vs round 1 buy phase) need calibrating
  against one VOD you eyeball.
- **VOD gaps are the real error source** — stream drops, disconnect-protection screens and
  removed segments all shift everything after them. The known Twitch VOD-chat desync bug has
  the same root cause.
- Broadcaster Stream Delay buffers the *live* feed, not the VOD. A local OBS delay does shift
  it.

Store a per-VOD `measured_offset_s` in `vod_calibration` the first time you verify a VOD by
any means, and reuse it for every other kill in that VOD. Sample twice, far apart, to detect
drift; if it is non-zero, store the slope and interpolate.

**Target: ±2s.** Pad clips to −8s/+4s around the kill and that is invisible.

### C6 — Output
Primary, and what klips.live ships: a deep link.

    https://www.twitch.tv/videos/<vod_id>?t=<offset-8>s

Plus a local page per match: the Twitch embed (`player.twitch.tv/?video=<id>&time=`) and a
side panel of clip markers labelled by `kill_type` — "Ace", "4K", "Clutch" — that reseek the
embed. No downloads, no storage, no grey area, and it is what you actually asked for.

Optional local export: `yt-dlp --download-sections` + `ffmpeg` to cut a file. Watch the
gotchas the vodkit project documents — fast seek can overshoot the requested time, Twitch
occasionally drops a fragment, and ffmpeg's seek offset needs care or audio lands wrong.
Keep it local and personal; bulk VOD downloading is against Twitch's ToS.

### C7 — Orchestration
A single scheduler process:

    every 30 min   C2 roster sweep
    nightly 04:00  C3 VOD index refresh, C1 match ingest, then C4 over new matches
    on demand      full re-resolve of a given match (CLI flag)

Notifications when an encounter is found: a Discord webhook. Same as klips.live's Plus tier.

---

## 4. Stack

Python 3.11+. It is where yt-dlp, ffmpeg bindings, OpenCV, Tesseract/EasyOCR and the prior-art
repos all live, and the OCR layer is the part with real risk — do not fight the ecosystem.

    httpx          async HTTP, both APIs
    sqlite3        storage, stdlib
    pydantic       parse the API responses, fail loudly on schema drift
    opencv-python  template matching, HUD ROI crops
    pytesseract    OCR (or easyocr if Tesseract underperforms on HUD glyphs)
    yt-dlp         VOD segment fetch
    ffmpeg         local clip cutting (external binary)
    typer          CLI
    apscheduler    the C7 scheduler

Config in `.env`: Henrik key, Twitch client ID/secret, your Riot ID, Discord webhook.

---

## 5. Build order

Each phase ends in something you can actually use.

**Phase 1 — see your own data. [BUILT]** C1 only. `python -m clipsvalorant ingest` pulls your
match history; `highlights` prints your aces, 4Ks, 3Ks and clutches with their millisecond
offsets. *Done when:* your last 20 competitive matches are in SQLite with correct per-kill
millisecond offsets. Logic is covered by 29 offline tests against a synthetic fixture;
verifying against the live API needs a HenrikDev key (see README).

**Phase 2 — the cheap path end to end.** Layer 1 + C5 + a plain-text C6. *Done when:* given
a match where you know a streamer was present, it emits a `?t=` link and the link lands on
your kill. **Measure the offset error here** — this number decides whether Phase 5 is needed.

**Phase 3 — the roster and the index.** C2 + C3. *Done when:* a local query answers "which
channels were broadcasting during match X" in under a second with no API calls.

**Phase 4 — free alias harvest + clip candidates.** Layer 3 + Layer 4. *Done when:* the alias
table has a few thousand rows from bio/title regex alone, and clip-derived candidates resolve
at least one match that Layer 1 missed.

**Phase 5 — fingerprinting.** Layer 5, score fingerprint first, killfeed OCR second. *Done
when:* it correctly identifies a streamer in a match you verified by hand, and its false
positive rate on 20 matches is acceptable.

**Phase 6 — the interface.** C6's embed-plus-marker-panel page, C7's scheduler, Discord
notifications.

Phases 1–2 are a weekend. Phase 3–4 are a weekend. Phase 5 is the open-ended one.

---

## 6. Risks and open questions

Resolve these empirically rather than by reasoning:

**tracker.gg is the oracle for highlight detection, not Riot's ceremony.** It states its
definitions in its own tag descriptions, and `tools/compare_tracker.py` scores us against
it: 49/50 competitive matches agree exactly. Its match data lives in the page's
`TRN_PINIA` store and needs a real browser, since `api.tracker.gg` 403s automated clients.
tracker has a `streams` field on every match object but it is `null` throughout, so it is
no help for the streamer problem.

**Resolved during Phase 1** (details in README): the v4 route is the two-segment form; the
Basic key gives 30 req/min; `kills[]` is top-level with millisecond offsets exactly as hoped;
`players[]` uses `team_id`. Also learned the hard way — an ace is five *distinct* opponents
(resurrection and respawn modes break raw counting), respawn modes file everything under
round 0, and Riot's `rounds[].ceremony` is one-per-round and awarded to the winning team, so
it corroborates but cannot be the oracle.

Still open:

1. **Is `video.created_at` really second 0?** Measure against one VOD by hand. Everything in
   C5 rests on this.
2. **What does `metadata.started_at` correspond to in-game?** One manual calibration.
3. **Actual VOD retention.** This caps your lookback and the whole pipeline's deadline.
   klips.live offers 60 days, which suggests either longer retention than commonly stated or
   that they index continuously. Check what you can actually still fetch at 7, 14, 30, 60 days.
4. **HUD OCR accuracy at streamer-realistic quality.** 720p, low bitrate, HUD-scaling
   differences, stream overlays covering the score. The single biggest unknown.
5. **Henrik's `stored-matches` coverage** for matches predating your use of the API.
6. **Henrik ToS** — ask in their Discord about this use case before running it nightly.
7. **Riot** — read-only API use carries no ban risk, and Riot's Vanguard FAQ lists the
   official API, game client and in-game APIs as expected to keep working; what they are
   blocking from Oct 6 2026 (grace to April 2027) is **memory access**, which we do not do.
   The local client endpoints remain undocumented and can change without notice.
8. **Keep it post-match only.** That property is what separates this from stream-sniping
   tooling, and it should be deliberate, not incidental. klips.live offers streamers an
   opt-out; worth mirroring if this ever leaves your machine.
