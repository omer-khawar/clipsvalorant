# clipsvalorant

Find your own Valorant plays in other people's Twitch VODs — when someone in your match
was streaming, jump straight to the moment you killed them, from their point of view.

A free personal replacement for [klips.live](https://klips.live/).
Full design in [PLAN.md](PLAN.md).

**Status: Phase 1 complete and validated against tracker.gg.** Match ingest works — it pulls your match history, stores
every kill with its millisecond offset, and finds your aces, 4Ks, 3Ks and clutches.
Phase 2 turns those offsets into Twitch VOD links.

---

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e .          # or: pip install httpx pydantic typer
cp .env.example .env
```

Then edit `.env`:

| Variable | What it is |
|---|---|
| `HENRIK_API_KEY` | HenrikDev API key — see below |
| `RIOT_NAME` / `RIOT_TAG` | Your Riot ID, split. `PlayerOne#NA1` → `PlayerOne` and `NA1` |
| `RIOT_REGION` | `eu`, `na`, `ap`, `kr`, `latam` or `br` |
| `DB_PATH` | Defaults to `data/clipsvalorant.db` |

### Getting a HenrikDev API key

Riot's own Valorant API is not an option: Riot issues no personal keys for Valorant, and
production keys require a working application plus a published ToS and privacy policy. So
match data comes from the HenrikDev unofficial API.

1. Join the HenrikDev Discord (linked from
   [their GitHub](https://github.com/Henrik-3/unofficial-valorant-api)) — key generation is
   gated behind joining.
2. Go to <https://api.henrikdev.xyz/dashboard/> → **API Keys** → **Generate New Key**.
3. A **Basic** key (30 requests/minute) is issued instantly and is more than enough here —
   Phase 1 uses about 21 calls to ingest 20 matches.

Their terms bar "big analytic projects" and require user consent. This is a personal tool
reading your own matches, which is within the spirit, but it is worth a ticket in their
Discord before you run it on a schedule.

---

## Usage

```bash
python -m clipsvalorant init
```
Creates the database and applies the schema.

```bash
python -m clipsvalorant ingest --limit 20
```
Fetches your recent competitive matches and stores them. Add `--mode unrated` or
`--mode ""` for other queues, `--refetch` to re-download ones you already have.

```bash
python -m clipsvalorant matches
python -m clipsvalorant highlights
python -m clipsvalorant highlights --kind ace
```

```bash
python -m clipsvalorant recompute
```
Re-derives kills and highlights from stored JSON with no API calls. `match.raw_json` is
kept precisely so detection logic can change without re-fetching 60 matches.

`highlights` is the Phase 1 payoff. The `time_in_match_ms` column is the number everything
downstream depends on — Phase 2 adds it to the match start time and subtracts the Twitch
VOD's `created_at` to get a seek offset.

```
Started (UTC)     Map         Rd Highlight          At  time_in_match_ms  Won
-----------------------------------------------------------------------------
2026-11-15 20:04  Ascent       2 Ace              3:30            210000  yes
2026-11-15 20:04  Ascent       4 3K               7:00            420000  yes
2026-11-15 20:04  Ascent       6 1v5 attempt     10:30            630000   no
```

### Without an API key

```bash
python -m clipsvalorant ingest-file tests/fixtures/match_v4_sample.json
```

Loads a saved match JSON from disk. Useful for working on the pipeline offline, and it is
how the test suite runs.

---

## Tests

```bash
.venv/Scripts/python.exe -m pytest -q
```

45 tests, no network. They run against two synthetic fixtures built by the `_generate*.py`
scripts in `tests/fixtures/`, with deliberately known answers:

- a clean ace (round 2), a 3K (round 4) and a 1v5 clutch attempt (round 6)
- six kills on five bodies via a resurrection — an ace with `kill_count` 5 (round 10)
- that same round is one you *lost*, carrying `CeremonyCloser` rather than `CeremonyAce`
- four kills on three bodies, which must stay a 3K and not become a 4K (round 12)
- a teamkill and a self-kill, neither of which may inflate a multi-kill (round 14)
- negative cases: an ace round that must not register as a clutch, a round where you died
  before becoming last alive, and a full Deathmatch that must yield nothing at all

---

## Notes on the data

### Verified against the live API (2026-11-17)

All of this was confirmed with a real Basic key against real matches:

- **Route:** `GET /valorant/v4/match/{affinity}/{match_id}` returns 200. The
  three-segment form with a platform returns 404, so the OpenAPI spec is the live one.
  The client tries the documented form first, so the fallback never costs a call.
- **Rate limit:** `x-ratelimit-limit: 30` on a Basic key, with a `ratelimit-policy` of
  `"per1min";q=30;w=60`. Ingesting 20 matches costs ~21 calls.
- **`kills[]` is top-level** and carries `time_in_match_in_ms`, `time_in_round_in_ms`,
  `round`, `killer`/`victim` as `{puuid, name, tag, team}`, plus `assistants`, `weapon`,
  `location`, `player_locations` and `secondary_fire_mode`.
- **`players[]` uses `team_id`** (and also has `team_number`).
- **`metadata`** has `game_length_in_ms`, `started_at` (ISO, sub-second precision), `map`,
  `queue`, `region`, `cluster`, plus `mvp`, `premier`, `season`, `platform`,
  `party_rr_penaltys`, `game_version` and `is_completed`, which we ignore.
- **`rounds[]`** additionally has `ceremony`, `ceremony_player`, `first_blood`, `plant`,
  `defuse` and per-player `stats`.
- `weapon` is sometimes a bare UUID rather than a name, apparently for ability kills. The
  model keeps whatever it gets; resolving UUIDs to names is a later concern.

### Validated against tracker.gg

`tools/compare_tracker.py` compares our highlights against tracker.gg's own per-match
tags, joined on match start time. tracker is the right oracle because it states its
definitions in the tag descriptions themselves, and `data/tracker/tracker_truth.txt`
holds a scraped snapshot so the comparison is reproducible.

**49 of 50 overlapping competitive matches now agree exactly** (it was 23/50 before the
fixes below). The one residual: a lost 1v1 on 2026-11-06 where we emit a `clutch_lost`
and tracker emits nothing. tracker's taxonomy only ever shows `clutchLost1v1` and no
lost 1v2/1v3 equivalent, so its lost-clutch coverage looks narrower than ours. Left as
a known difference rather than contorting the model to match.

Getting tracker's data out needs a real browser: `api.tracker.gg` returns 403 to
automated clients, and the match list lives in the page's own `TRN_PINIA` store.

### How a streamer is confirmed, and why the obvious test is not enough

Finding the channel is the easy half. Proving it is the right person is the whole problem.

The natural test — does this channel have a past broadcast that spans my match? — is worth
almost nothing on its own. It asks whether somebody was **broadcasting**, never what was on
screen, and a channel that streams daily passes it for any match ever played. A lobby-mate
called `Curry#2622` was duly matched to `twitch.tv/curry`, a radiant Valorant streamer, on an
exact name match plus a broadcast that covered the match. Wrong person: the real Curry was a
friend with no channel, and the broadcast in question was titled *SECRET DEADLOCK STREAM* —
a different game entirely. Riot IDs are 3–16 characters, so every short one already belongs
to somebody on Twitch.

So the layers that *find* candidates are kept strictly separate from the rule that **confirms**
them. A candidate is confirmed only by evidence tying the channel to that particular match:

- **a Valorant clip cut from the covering VOD at an offset inside the match.** Clips carry
  `video_id`, `vod_offset` and `game_id`, which makes them the only way to reconstruct what a
  past broadcast contained: `GET /helix/videos` carries no game id, and `GET /helix/streams`
  only answers for channels that are live right now.
- **a Riot ID the channel advertises itself**, in its bio or a VOD title, with the tagline
  agreeing and not just the name.

A candidate is ruled out when clips exist for that broadcast and none are Valorant, when every
Riot ID the channel advertises has a different tagline, or when no broadcast holds at least 60%
of the match.

Everything else — an exact name match and a broadcast spanning the match included — is printed
as **unverified**, under a heading saying so, and is never written to the alias table. That last
part matters: the alias table is consulted first on every later match, so one wrong row is
permanent and comes back looking like corroboration.

The broadcast title is always printed. It is free, and it is what would have caught the
collision above.

### The real bottleneck is VOD retention, not identity

Measured over 44 matches (every one inside the nominal 14-day window), 406 players, 9,437
Valorant clips from 5,810 channels and 5,810 bios read:

| | |
|---|---|
| candidates with **no surviving broadcast at all** | 385 of 391 |
| channels seen that keep **any** archived VOD | 12 of 4,246 |
| confirmed | 1 |

So the identity funnel is not what limits the hit rate — the archive is. Twitch keeps past
broadcasts for 7 days, 14 for Affiliates, Partners and Turbo subscribers, and only if the
streamer has VOD saving switched on at all. Most channels keep nothing: plenty have highlights
and uploads but zero `type=archive` videos, which is the only kind with a usable timeline.

The consequence is operational. Resolution is retroactive — no live polling is needed, because
a VOD can be looked up days later — but the window is days, not weeks. A match resolved a
fortnight after it was played usually has nothing left to link to, however well the player is
identified. Run `resolve` within a few days of playing, or on a schedule, and the same matches
that come back empty now would have resolved while the VODs existed.

### Four things that make highlight detection subtle

Every one of these came from real matches where our output disagreed with a source, and
in each case the source was right:

1. **Respawn modes report every kill under round 0.** Deathmatch, Escalation and Snowball
   are excluded by `Match.has_rounds`, which checks the queue name and falls back to
   "more than one round" for unrecognised queues. Without it, one Deathmatch produced
   **12 phantom aces**.
2. **Multi-kills count raw kills, not distinct bodies.** tracker has a `kills6k` tag,
   described as "Killed 6 enemies in one round" - impossible with five distinct enemies,
   so a resurrection makes the sixth. We label by raw opponent kills (3K/4K/Ace/6K) to
   match tracker, and keep `distinct_victims` alongside because it is the more meaningful
   number. Teamkills and self-kills never count; tracker's tags say "enemies".
3. **A clutch has no minimum kill count.** tracker's `clutch1v1` is "Won a 1v1 clutch",
   which fires on winning the round however you won it. Requiring two kills missed most
   of them. Won and lost are separate kinds (`clutch` / `clutch_lost`).
4. **Revives mean "died once" is not "dead now".** Sage resurrects and Clove self-revives,
   so liveness has to be modelled on the timeline: a player is dead if their most recent
   death was not followed by a kill of theirs. Three separate bugs came out of this - a
   revived enemy counted as dead (making a 1v2 look like a 1v1), a blanket "revived means
   never dead" rule that then got it wrong in the other direction when the player died a
   *second* time, and our own revive disqualifying us from a clutch we actually won.
   `revived_in_round` and `_dead_at` in `kills.py` handle it. Known limitation: a revived
   player who never kills again still looks dead to us.

**Riot's `rounds[].ceremony` is not usable as ground truth**, which is why tracker is.
There is one ceremony per round and it goes to the *winning* team, so killing all five
enemies and still losing the round to a detonation yields `CeremonyCloser`, not
`CeremonyAce`. It is stored on each highlight as corroboration only.

## Notes on the schema

**The models are deliberately permissive.** HenrikDev does not publish the v4 match
response shape; their docs site has a stub and their OpenAPI spec confirms the fields we
depend on but not every nested object. So `clipsvalorant/models.py` ignores unknown fields
and coerces anything that might be a bare string or an `{id, name}` object. The raw payload
is stored in `match.raw_json` as the source of truth.

**Confirmed fields** (from `https://api.henrikdev.xyz/openapi.json`):
`metadata.{match_id, started_at, game_length_in_ms}`, `players[].{puuid, name, tag}`, and a
top-level `kills[]` carrying `{time_in_match_in_ms, time_in_round_in_ms, round, killer,
victim, assistants}` where killer/victim are `{puuid, name, tag, team}`.

**Two things may need adjusting on your first live call**, and the code handles both
defensively rather than assuming:
- The v4 match route. Their OpenAPI spec gives `/valorant/v4/match/{affinity}/{match_id}`;
  their changelog shows a three-segment form with a platform. The client tries the first and
  falls back to the second on a 404.
- `players[].team_id` vs `team`. Both are accepted.

**`stored-matches` is not a mirror.** It is an accumulating subset populated on request, so
matches nobody has ever fetched are absent. Your own history fills in once you start using
it — if `ingest` lists nothing, that is why.

## Credentials

The project ships none and stores none in the repository. Every user brings their own:

- a **HenrikDev** API key, free from their dashboard — the Basic tier (30 requests/minute) is
  enough;
- a **Twitch application** client id and secret, from `dev.twitch.tv/console/apps`. Registering
  an app requires 2FA on the Twitch account. Only an app access token is used, so there is no
  user login and no OAuth scopes.

Both live in `.env`, which is gitignored. `.env.example` shows the shape with placeholders.
Use `python -m clipsvalorant set-secret` to write the Twitch secret through a hidden prompt, so
it never reaches shell history or terminal scrollback. The local SQLite database holds other
players' Riot IDs and PUUIDs from your own match history, so `data/` is gitignored too — don't
publish it.

## License

[MIT](LICENSE).

## Disclaimers

This is a free, non-commercial hobby project. It is not affiliated with, endorsed by, or
sponsored by Riot Games, Twitch, or klips.live.

> clipsvalorant isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot
> Games or anyone officially involved in producing or managing Riot Games properties. Riot
> Games, and all associated properties are trademarks or registered trademarks of Riot Games,
> Inc.

Twitch is a trademark of Twitch Interactive, Inc. No Riot or Twitch logos, artwork or other
game assets are included in this repository.

Valorant match data comes from the [unofficial Valorant API by
HenrikDev](https://github.com/Henrik-3/unofficial-valorant-api) — thanks to that project, which
is what makes any of this possible without a Riot production key. Its Basic tier is documented
for private and educational projects and is not intended for production use; respect its rate
limits and get an Enhanced key before pointing anything high-volume at it.

Twitch data comes from the official Helix API. The tool reads metadata and emits ordinary
`twitch.tv/videos/<id>?t=` links. It does not download, re-host, re-encode or embed anyone's
video, and it does not create clips — `POST /helix/videos/clips` needs the broadcaster's own
token, which is the constraint that shapes the whole output format.

Match data describes real people. Only ever point it at your own match history.
