# clipsvalorant

Type a Riot ID and scan. One HenrikDev key runs it, so scans take turns in a queue
rather than all at once; the page tells you where you are in the line. It runs locally
with `python -m clipsvalorant serve`, and [deploy/ORACLE.md](deploy/ORACLE.md) puts the
same thing on a free virtual machine with HTTPS.

> **The page is called val:grind** — *your play, their POV*. The brand lives in the web
> UI: the mark, the wordmark, the splash and the queue screen. The package, the CLI and
> this repository keep the name `clipsvalorant`, because renaming an import path buys
> nothing a logo does not already do.

Find your own Valorant plays in other people's Twitch VODs — when someone in your match
was streaming, jump straight to the moment you killed them, from their point of view.

A free personal replacement for [klips.live](https://klips.live/).
Full design in [PLAN.md](PLAN.md).

**Status: working end to end.** Match ingest is validated against tracker.gg — it pulls
your match history, stores every kill with its millisecond offset, and finds your aces,
4Ks, 3Ks and clutches. The streamer search turns those offsets into Twitch VOD links and
confirms them by reading the broadcast itself. `python -m clipsvalorant serve` puts the
whole thing behind a page with a player in it.

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
| `CLIPS_PASSWORD` | Optional, and empty is right for local use. One shared password for the web UI, needed only if you bind it past localhost — see [Deploying it](#deploying-it) |

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
python -m clipsvalorant serve
```
The web UI, and the easiest way in. Opens <http://localhost:8765/>: type a Riot ID,
press Search, and the moments it finds play in an embedded Twitch player right on the
page — no copying timestamps into the address bar.

Opening the page costs nothing. The clip list is read straight out of SQLite, so
everything found on earlier runs is there instantly; only pressing **Search** goes out
to Riot and Twitch. A progress bar fills match by match while that runs — reading a
broadcast takes long enough that a silent minute looks like a hang — and the narration
behind it is one click away under *log*.

**A search looks back 14 days by default.** That is not a round number, it is the whole
of what Twitch still holds: 7 days of past broadcasts for most channels, 14 for
Affiliates, Partners and Turbo subscribers. A shorter default would quietly skip
matches that are still findable; a longer one would spend API calls on broadcasts that
have already been deleted. A match outside the window is never fetched at all — the
listing already carries its date, and the detail call is the expensive half — and the
page says why rather than showing an empty result, because *nothing found* and *nothing left
to find* look identical and mean very different things.

**Highlights are a suggestion, not the only way in.** The featured list is the few plays
worth opening with — aces, clutches, multi-kills, best first — but underneath it the
whole match is reachable: every round under *Rounds*, every one of your kills under
*Kills*, and a scrubber across the top that you can click anywhere on. Rounds are dated
the same way calibration dates them, from the two clocks Riot puts on every kill, so
they are measured rather than guessed. A round in which nobody died at all has no kill
to carry those clocks and so is absent, rather than interpolated from its neighbours —
guessing a timestamp is precisely what put every link a round and a half early the
first time.

The page follows the system light or dark setting and the toggle in the corner
overrides it, remembered per browser.

Three more things are worth knowing about:

- **Each account gets its own database.** `is_me` is a column, not a query — kills,
  highlights and the whole notion of *your* plays are derived from it when a match is
  stored — so two accounts sharing one file would mean one of them silently owning the
  other's highlights. Searching `Someone#ELSE` writes `data/ui-someone-else.db`, and
  the dropdown lists every account searched on this machine.
- **Only confirmed and likely encounters get clips.** An *unconfirmed* row is not a weak
  judgement, it is the absence of one — nothing says the broadcast was even Valorant.
  Giving those timestamps makes them indistinguishable on screen from a streamer we
  actually found, which is how `twitch.tv/solly17x` came to be listed with five playable
  moments of a stream that was Elden Ring from end to end. `python -m clipsvalorant
  audit` now re-reads those rows and rules them out. The evidence behind every verdict
  is one click away under *why we think this is them*.
- **A moment outside the broadcast is shown, not hidden.** A stream that began at round
  14 cannot contain round 2, and one that ended early cannot contain the last round.
  Both say so on the button instead of linking somewhere wrong — Twitch answers an
  over-long offset with the last frame and no error, which looks exactly like a bad
  detection.

A marker sits on the moment itself; playback starts eight seconds earlier. A kill is
the end of a play rather than the start of one, so landing exactly on the timestamp
means arriving after the interesting part — but drawing the marker eight seconds early
would misplace it on the scrubber. The two numbers are kept apart for that reason.

The server binds to localhost only. The page has no authentication, and anything that
reached it could spend this machine's API quota.

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

## Deploying it

`serve` is a standard-library HTTP server, which rules out the serverless hosts: there is
no WSGI app to hand them, and a scan is a background thread that outlives the request
that started it by several minutes. What it needs is an ordinary process with a disk.

**[deploy/ORACLE.md](deploy/ORACLE.md) is the current recipe** and the one that is
maintained: an Oracle Always Free virtual machine, Docker Compose, and Caddy for a real
certificate. It is free rather than nearly free, it is a runbook rather than a sketch,
and every number in it was measured on the machine it describes. Read that; the rest of
this section is the history and the parts that are true of any host.

A hosted copy is better off than a local one in one respect: Twitch's embedded player
wants a real hostname in its `parent` parameter, and `localhost` only works because
Twitch makes an exception for it.

`fly.toml` is still here and still works. Fly is where this ran first, and the move off
it was not Fly's fault — Oracle's free tier is a whole machine with a disk, and this
needs CPU more than it needs anything else.

### The queue

Several people, one HenrikDev key. Running every search as it arrives does not get them
done sooner — they share the same requests per minute and all crawl — and refusing the
second one, which is what this did when it only ever had one user, tells somebody to come
back with no idea when. So scans take turns, one at a time, and the page says where you
are in the line.

What that means in the code:

- `Queue` owns a FIFO of jobs and a single worker thread. `WORKERS = 1` is not a
  placeholder; it is the API key's rate limit expressed as a number.
- Submitting returns a **ticket** — an unguessable id the browser keeps in
  `localStorage`. It is how `/api/job` tells your scan from someone else's, how a reload
  finds your place again, and how `/api/cancel` refuses to drop a scan you do not own.
- One scan in flight per person, by address. It is a courtesy, not a boundary: the
  address comes from `Fly-Client-IP`, which a determined client can set. It stops an
  impatient refresh from filling a queue everyone shares, which is the thing that
  actually happens.
- `/api/queue` needs no ticket at all. It is what the header chip and the waiting screen
  read to show that the line is moving.
- The queue is in memory on purpose. One that survived a restart would hand people back
  a position in a line whose running scan died with the process.

### The password

`CLIPS_PASSWORD` gates every route but `/healthz` behind one shared password — any
username, since there are no accounts to tell apart. Unset, the page is open to anyone
with the link, and `serve` says so at startup rather than leaving you to find out.

Leaving it off is a real trade and worth naming: the HenrikDev key is yours, the quota
spent is yours, and HenrikDev's terms bar "big analytic projects" and ask for user
consent. A password is what keeps a hosted copy recognisably a personal tool. On the Oracle
deploy it lives in `deploy/.env`, which is not committed, and changing it is an edit and
a restart:

```bash
sudo systemctl restart clipsvalorant
```

On Fly it is a secret and needs no redeploy:

```bash
fly secrets set CLIPS_PASSWORD=something -a your-app-name
```

### First deploy, the Fly way

Kept because `fly.toml` is kept, and because a managed host is the right answer for
somebody who does not want to own a machine. For the maintained path, see
[deploy/ORACLE.md](deploy/ORACLE.md).

```bash
pwsh -Command "iwr https://fly.io/install.ps1 -useb | iex"
```
Installs `flyctl`. Then `fly auth signup` (or `fly auth login`) — Fly wants a card on
file even for small machines.

```bash
fly apps create clipsvalorant
```
App names are global, so if that one is taken, pick another and change `app` in
`fly.toml` to match.

```bash
fly volumes create clipsvalorant_data -r iad -s 3 -y
```
Three gigabytes for the databases, the artwork cache and the VOD frames. `-y`
acknowledges that a single volume lives on a single host: lose it and you lose a cache,
which is all of it that cannot be rebuilt by running the scan again. Change `-r` if `iad`
is not near you, and `primary_region` in `fly.toml` with it.

```bash
fly secrets set HENRIK_API_KEY=HDEV-... TWITCH_CLIENT_ID=... TWITCH_CLIENT_SECRET=... RIOT_NAME=... RIOT_TAG=... CLIPS_PASSWORD=...
```
The Riot ID is in here rather than in `fly.toml` for the same reason `data/` is
gitignored: `fly.toml` is committed, and your Riot ID is not the repository's business.
`RIOT_REGION` is in `fly.toml`, where it is visible and harmless.

```bash
fly deploy --remote-only --ha=false
```
`--remote-only` builds the image on Fly's builder, so Docker is not needed locally.
`--ha=false` keeps it at one machine: two would each hold their own copy of the SQLite
file and their own "one search at a time" lock, so which half of your results you saw
would depend on which machine the proxy picked.

Then `fly open`. After that, every change is
`fly deploy --remote-only`.

### What is different up there

- **The machine stays up.** `min_machines_running = 1`, because the queue is in memory:
  a machine that scaled to zero would take the line with it and kill the scan that was
  running. That is a few dollars a month rather than nothing, and it is the price of a
  URL other people can use.
- **OCR's models load on first use**, so the first scan after a deploy has a slower
  start than the ones after it.
- **The line is shared, and so is the key.** Everyone's scans go through one HenrikDev
  key at 30 requests a minute. A bigger tier makes each scan faster; it does not make
  them parallel, because `WORKERS` is still 1. Raise it only if the key's limit really
  has moved, and expect the machine's CPU to become the next ceiling.
- **A shared CPU is a slow OCR.** Confirmation reads frames through ffmpeg and
  onnxruntime, and `shared-cpu-1x` does that several times slower than your desktop.
  `fly scale vm shared-cpu-2x` if scans drag; remove the `[[vm]]` block from `fly.toml`
  first, or the next deploy resets the size.

---

## Tests

```bash
.venv/Scripts/python.exe -m pytest -q
```

348 tests, no network. They run against two synthetic fixtures built by the
`_generate*.py` scripts in `tests/fixtures/`, with deliberately known answers:

- a clean ace (round 2), a 3K (round 4) and a 1v5 clutch attempt (round 6)
- six kills on five bodies via a resurrection — an ace with `kill_count` 5 (round 10)
- that same round is one you *lost*, carrying `CeremonyCloser` rather than `CeremonyAce`
- four kills on three bodies, which must stay a 3K and not become a 4K (round 12)
- a teamkill and a self-kill, neither of which may inflate a multi-kill (round 14)
- negative cases: an ace round that must not register as a clutch, a round where you died
  before becoming last alive, and a full Deathmatch that must yield nothing at all

---

## What one scan actually covers

Two settings decide which matches a scan can possibly find anything in, and for a long
while both were narrower than they looked and neither said so.

**The window is walked, not asked for.** HenrikDev's stored-matches listing pages, and
a hundred matches is the largest page it returns. Reading only page one meant the window
never decided the depth — a hundred matches did, which for an active account is about
three weeks. Choosing 30 days returned 100 of 144 matches and reported the 100 as though
they were all of it. `_list_window` now walks pages until one crosses the cutoff, the
history runs out, or the ceiling stops it, and `IngestReport.complete` says which, so
the page can tell you when it stopped short rather than leaving you to notice.

Two things it has to get right, both of them tested. The page size cannot change
mid-walk: which matches page two holds depends on the size page one was asked with, so
shrinking it to fit the remaining ceiling returns a page that overlaps the one already
read — and that overlap is indistinguishable from the end of the history. And a full
page of matches already seen means `page` was accepted and ignored, which is not the
end of the history either, and is reported as incomplete.

**The queue filter hides more than you would guess.** Scans default to Competitive, and
nothing used to say so. On one real account that is 45 of 69 matches in a fortnight:

| queue | matches in 14 days |
|---|---|
| Competitive | 45 |
| Swiftplay | 8 |
| Deathmatch | 8 |
| Custom Game | 4 |
| Team Deathmatch | 3 |
| Gauntlet: Glitched | 1 |

A custom game is exactly where a scrim against a streamer lives, and one of those
hidden Swiftplay matches had a Paper Rex player in the lobby. So a friend who says he
played against a pro and a scan that comes back empty can both be telling the truth —
which is indistinguishable from a broken scanner, and cost several days of hunting a
bug in the identity funnel that was never there. The page has a Queue control now, and
a filtered scan names what it is holding back and how to include it. The default stays
Competitive: every queue is half again as many matches to resolve, and on a small
machine that is a real wait, so it should be a choice rather than a surprise either way.

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

### The two clocks, and the bug that lived between them

A match carries two kinds of time and they do not share a zero.
`metadata.started_at` is wall-clock — the moment the game server session opens, which
is when the queue pops and agent select appears. `time_in_match_in_ms`, on every kill,
counts from when the map finishes loading. Agent select and loading sit between them.

Adding the two together is the obvious thing to do, and it is what this project did
until it was checked against actual video. Every link came out about a round and a half
early. Nothing errored. The link opened the right broadcast, on the right map, inside
the right match, and the play it promised had not happened yet — which is why it
survived so long: it looks exactly like a correct link, and the detection behind it
genuinely was correct.

The gap is not a constant. Measured by reading the screen:

| Broadcast | Map | Gap |
|---|---|---|
| twitch.tv/vaerie | Corrode | 101s |
| twitch.tv/cocapuppy | Sunset | 109s |
| twitch.tv/yayster | Bind | 125s |
| twitch.tv/yayster | Haven | 79s |

Agent select is fixed; the rest is ten people's machines loading a map, and that is the
spread. A single constant would leave a link up to twenty-odd seconds out, which on a
3K is the difference between the fight and the walk towards it.

So it is measured per match, per broadcast, in `calibrate.py`. Valorant draws a
countdown at the top centre of the screen in every round — the buy phase counts down to
the barriers dropping, the round itself counts down from 1:40 — and the kill data gives
the exact moment each round began on the in-match clock. Read the countdown off a frame
and the frame is dated; the difference between where it sits in the broadcast and where
it sits in the match is the number.

One frame cannot say *which* round it is looking at, so each one offers an answer per
round and the answer that keeps recurring across frames wins. A shift that is out by a
whole round would have to fit every frame at once, and real rounds do not run to a
fixed length — 85s, then 74s, then 90s — so it cannot. In practice five to eight frames
agree to within a second.

Except that some frames do say which round it is. Valorant draws **ROUND 7 //////
ATTACKERS** above the buy-phase banner, and the OCR reads it: of four frames pulled by
hand, three came back as `ROUND3`, `ROUND4` and `ROUND 10`, and the fourth mangled the
digit into the slashes beside it. A frame that names its own round is dated outright
rather than put to a vote, and one of them is worth more than the whole ballot — read
against `twitch.tv/sllumtackss`'s Corrode broadcast, the single `ROUND 10` frame gave
115.96s where thirteen frames of voting had settled on 115.9.

It also refuses to date a frame that cannot belong to this match. The frame the page
linked to on `twitch.tv/NeonQuin` read `ROUND 3` at 0:00, which puts the start of the
match 764 seconds from where its own timestamp does — outside any plausible agent
select, so no shift is offered at all. The voting had no such scruple and returned
110s, which is a perfectly ordinary-looking number arrived at from somebody else's game.

Frames are also now *sampled* at the corrected offset rather than the naive one. A pass
looking for lobby names aims at round ends, where the scoreboard shows all ten at once,
and it was aiming a buy phase and most of a round early — reliably missing the one frame
worth the most. Same ten frames, same cost, pointed at the right seconds; it is part of
why the same streamer could be confirmed on one run and not the next.

```bash
python -m clipsvalorant calibrate
```
Measures every confirmed encounter not yet measured. Costs frames, not API calls. New
resolves do it automatically; this is for links found before the module existed.

Where it cannot be measured — no OCR installed, or a stream that joined too late to
show a countdown — the 109s median stands in, and the UI says so on the card rather
than presenting a guess as a measurement.

### Stopping a scan, and the region that was never checked

A scan is mostly waiting - for HenrikDev's rate-limit window, for Twitch, for
ffmpeg, for OCR - and none of that waiting could be interrupted. Start one on the
wrong region and the only way out was killing the server.

Two things fix it. Every `time.sleep` on a waiting path became
`threading.Event.wait(timeout)`, which is a sleep that can be woken: a two-minute
quota pause now ends on the press rather than two minutes later. And the scan checks
a flag between matches and between candidates, so what it stops on is a whole match,
never a half-judged one. Everything already written stays written - a scan that
checked thirty matches really did check them, and throwing that away would make
stopping expensive enough that nobody would do it. Measured: a running scan stopped
6.6s after the request, which is one in-flight match finishing.

The deeper bug was why a wrong region ran at all. A Riot ID is global; matches are
not. The stored-matches endpoint does **not** object when you ask the wrong shard
about somebody - it answers, with their real matches. A search for a European player
on NA ran for seven minutes and filed forty matches, fifteen of which recorded their
cluster as *Frankfurt*, while the `region` column said `na` because we had stamped
the requested region onto them.

So a scan now asks `/valorant/v1/account/{name}/{tag}` first, which knows where an
account actually plays, and believes that over the dropdown:

```
Looking up aliii#2008 in NA...
aliii#2008 plays on EU, not NA - searching EU instead.
```

One call, and it saves the whole run. If the lookup fails or returns something
unfamiliar, the requested region is kept: a scan on the region you picked is a
normal outcome, one silently moved somewhere nobody asked for is a surprise.

### The detail we were throwing away

`raw_json` is stored as the source of truth so that detection logic can change
without re-fetching. For a long while what actually reached the disk was this
project's own Pydantic dump — and the model is declared `extra="ignore"`, so every
field it had not yet learned about was dropped on the way in.

The v4 match endpoint had been sending all of this the whole time:

| | what it carries |
|---|---|
| `kills[].location` | where the victim fell, in world coordinates |
| `kills[].player_locations` | where all nine others were standing, and facing |
| `rounds[].plant` / `.defuse` | site, time into the round, position |
| `rounds[].winning_team_role` | attacker or defender, which the team alone cannot say |
| `players[].stats.damage` | dealt and received |
| `players[].stats.headshots` | with body and leg, so accuracy is derivable |
| `players[].tier` | the rank they played at |
| `metadata.map.id`, `players[].agent.id` | the uuids the asset service is keyed on |

Sixty-seven matches had been stored without any of it. Nothing local could recover
them, so `clipsvalorant backfill` re-fetches a trimmed match from the API — one call
each, which is why it is a command of its own rather than something a scan does
quietly. A `Match` now carries the payload it was parsed from, so "source of truth"
is true.

### Putting a kill on a map

Riot's asset service publishes, per map, a top-down minimap and four numbers that
convert world coordinates into a point on it. The conversion has one trap: the
game's **y** drives the image's **x**.

```
image_x = game_y * xMultiplier + xScalarToAdd
image_y = game_x * yMultiplier + yScalarToAdd
```

Get that the wrong way round and you still get a map covered in plausible dots,
every one of them mirrored into a wrong room — which is the worst kind of bug this
project can ship, since the whole claim is that its positions and timestamps are
true. It is pinned by a test that moves along one axis at a time.

Checked against a real Abyss match: 150 of 150 kills land inside the image. On the
Corrode match in the UI, the map draws 127 dots, of which 17 are the account's kills
and 7 its deaths — matching that scoreboard's 17/7 exactly.

Every dot carries its own VOD offset, so clicking a kill on the map opens the
broadcast at the moment it happened. The positions belong to the match, but where
they land inside a broadcast differs per streamer, so each find gets its own copy.

Artwork is fetched once and served from `data/assets/` rather than linked. A page
embedding `media.valorant-api.com` directly would hand that CDN the viewer's address
and the list of maps and agents they play, every time it was opened, for a tool that
otherwise speaks only to localhost and Twitch. Ids coming back from the page are
matched against a UUID pattern before they touch the filesystem, and a miss is never
an error: no artwork, no catalogue, no internet all end as "draw the name instead".

Rounds are numbered from zero by Riot and from one by people. Everything shown to a
reader goes through a single helper, so the round strip, the tooltips, the map and
the moment list cannot disagree with each other.

### Where the page came from

The page is one file — `PAGE` in `clipsvalorant/web.py` — with no build step, no
framework and no CDN, because the server is a stdlib `ThreadingHTTPServer` and the
whole point is that it starts with one command.

Its design is taken from [wundercorp/awesome-components][awesome]. That catalogue is
worth being precise about: it indexes 6,591 components, and **none of them ships any
source code** — every `prompt.md` in it says "No reference source files were
available". What it does ship is a rendered DOM snapshot of each component and a
screenshot. The snapshot is the useful part, because a post-render Tailwind class
string is a measurement: `rounded-2xl`, `border-neutral-900/10`, `p-5`,
`tracking-[0.35em]`, `shadow-[0_10px_40px_rgba(0,0,0,0.04)]` are exact values, not
impressions. Those were read off and reimplemented in plain CSS.

| Part of the page | Taken from |
|---|---|
| Shell, dot-grid, hairline rules, card system, modular spans | `reapollo/bento-monochrome` |
| Metric tiles | `reapollo/glassmorphism-minimal-metrics-block-shadcnui` |
| Three-way theme control | `ncdai/theme-switcher-1` |
| Window and filter controls | `ruixenui/segmented-button-group` |
| Scrubber ticks and round-jump buttons | `ruixenui/audio-timeline-with-chapters` |
| Match cards, round strip, map and scoreboard panels | `reapollo/bento-monochrome` (its spans and card rules) |
| Riot ID field | `kokonutd/action-search-bar` |
| Scan progress | `educalvolpz/animated-progress-bar` |

The background shader is not from the catalogue — there is no shader source anywhere
in it, `gl_FragColor` appears zero times across all 6,591 entries. It is a WebGL
domain-warped fbm written here, deliberately greyscale and low-contrast, following the
monochrome silk of `reuno-ui/background-paper-shaders`. It runs at 30fps, caps device
pixel ratio at 1.25, stops when the tab is hidden, freezes under
`prefers-reduced-motion`, and if WebGL is missing the canvas simply hides — the
dot-grid and vignette carry the background on their own.

The dot-grid is the one piece doing real work rather than decoration. It is a 20px
grid masked down to dashes, and every element on the page sits on it: a 12-column
layout, 12px gutters, a 1152px measure, vertical rhythm in multiples of four, and
tabular figures everywhere a number appears.

[awesome]: https://github.com/wundercorp/awesome-components

### Where a scan spends its time

Measured before anything was changed, because every guess about this was wrong.

A frame of a broadcast costs about 2.1s to fetch and another 0.7-5s to read, the
spread depending entirely on how much text is on screen: a quiet frame returns five
text boxes, a cluttered one with a chat overlay returns a hundred, and OCR charges per
box. The comment in `watch.py` claimed roughly three seconds per frame all in. It was
nearer eleven, and four fifths of that was OCR, not the network.

But frames were never the bulk of it. A lobby needs one Twitch name search per player
and one broadcast listing per candidate — eighteen round trips for a single match —
and every one of them was issued on its own while the process sat idle.

| | before | after |
|---|---|---|
| A nine-player lobby's lookups, at a 250ms round trip | 4.52s | 1.01s |
| One pass of eight frames over a broadcast | 35.6s | 25.7s |
| Match details fetched to resolve a 14-day window | all 40 listed | only those inside it |

The last row was a regression of my own: raising the fetch to cover a fortnight meant
paying for the detail of every match listed, including the ones the window would throw
away. HenrikDev charges a point per call plus one per Riot request it triggers behind
the scenes, and sleeps out the window when it runs dry, so those were not merely wasted
calls but wasted minutes.

A second round of measurement, after the above, found the longest single stall was
not in either list. Opening a broadcast means asking `yt-dlp` for its HLS URL, which
costs 2.6s — and 1.25s of that is `yt-dlp` starting Python up, before it has touched
the network. It was paid once per candidate, in a row, inside the loop. The VODs a
match needs are all known before any of them is opened, so they are now resolved
together.

That left OCR, and OCR turned out to have no settings worth changing. Each of these
was measured over twelve real frames, comparing the text that came back and not just
the clock:

| | speed | text |
|---|---|---|
| Larger recognition batches (24) | **0.61x** — slower | 65 strings lost |
| Larger recognition batches (48) | **0.38x** — slower | 95 strings lost |
| Smaller detection side (640px) | 0.99x | 59 strings lost |
| Skipping the angle-classifier pass | 1.05x | 2 lost, 8 gained |

Bigger batches are *slower* because every crop in a batch is padded to the widest one
in it. Nothing here buys speed without changing what is read, so none of it was taken.

What was left is not reading the same frame twice. Frames were already cached on disk
so that a re-run downloads nothing; now what was read off them is cached beside them,
which is most of what a sweep over the same fortnight was spending its time
rediscovering. A frame's text never changes, so this cannot move a verdict.

| | before | after |
|---|---|---|
| Four broadcasts' playlists | 9.11s | 2.98s |
| Twelve frames, looked at a second time | 15.9s | 0.08s |

Three things that sound like savings and are not. OCR does not thread — onnxruntime
already uses every core, so running frames in parallel made a pass *slower* (8.9s to
12.9s on six frames). Calibrating from frames the confirmation pass already read
rarely fires, because a streamer confirmed from the screen is usually confirmed on the
first frame, leaving nothing to reuse. And none of the OCR engine's own knobs help, as
above. All three are kept or left alone because they cost nothing when they miss, but
none of them is where the time went.

What is left is the shape of the problem rather than an inefficiency: klips.live is
fast partly because its alias database is shared across everybody who uses it, so most
lobbies are a lookup rather than a search. This one starts empty and per-account, and
only `alias` rows accumulated from previous runs close that gap.

#### And then the part that was not the code at all

Every saving above was measured on one machine. The same code then moved to another and
ran seven times slower, and two days went into looking for the bug. There was no bug:

| | 12-core laptop | 1/8 OCPU free tier |
|---|---|---|
| One frame, fetched and read | ~1.6s | ~13.2s |
| One match, end to end | ~4.7s | ~34s |
| 100 matches | 461s | over 50 minutes |

Same code, same keys, same queue depth. The laptop figures come from the `ingested_at`
and `last_resolved_at` columns of a real 100-match scan, which is why they can be
checked rather than remembered. Layer 5 is OCR and OCR is pure CPU, so an eighth of a
core is exactly what it sounds like.

Worth writing down because of how it presents: a scan that takes an hour, pauses for a
minute at a time while a rate limit refills, and finishes with fewer confirmations than
you expected looks precisely like a scanner that is broken. It is worth knowing which
machine a timing came off before believing a profile.

#### The scan that had nothing left to find, and did it all again

The sweep has always known which matches are worth resolving: never the ones past
Twitch's retention, never the ones already confirmed, never the ones looked at a few
hours ago. `sweep.matches_due` is that policy and it is twenty lines long.

The page did not use it. Pressing Search resolved **every match in the window, every
time** — a name search per player and a broadcast listing per candidate, a few hundred
Twitch calls per scan, to arrive back at what was already on disk. Reported as a
30-minute scan of `razor#d4c`, an account that had been scanned several times already.

Against this repository's own `razor#d4c` database, with a 14-day window:

| | matches resolved |
|---|---|
| Re-run on the day it was last scanned, before | 44 of 44 |
| Re-run on the day it was last scanned, now | **0** of 44 |
| Re-run two days later, now | 10 of 39 |

The second thing that run did was worse than slow. Twitch deletes past broadcasts after
7 to 14 days, so a re-examination of an old match reaches "no VOD holds enough of it"
and writes *rejected* over whatever was there — including `twitch.tv/cocapuppy`,
confirmed off ten of ten lobby names on the first frame. That is a fact about retention
and not about the lobby, the confirmation it overwrites can never be made again because
the video is gone, and `_remember_encounter` now refuses it. A rejection we actually
learned something to reach is still written down.

#### Reading five frames instead of ten

Most candidates are the wrong person — 2,486 of 2,490 encounters in that database — and
each one was read to the full ten-frame budget before being filed. Once five frames have
come back with Valorant on screen and none of the lobby in it, five more cannot change
the answer, so the pass stops there.

Measured on one match of `yrn#wkmts`'s, same machine, same lobby, both from a cold frame
cache:

| | before | after |
|---|---|---|
| Wall clock | 52s | **44s** |
| VOD frames read | 20 | **10** |
| Confirmed | 1, and it was the wrong person | 0 |

#### Eight accounts, end to end (2026-11-21)

Every run below went through the web UI's own path on this machine: a 14-day
window, Competitive only, each account's database in whatever state earlier
scans had left it.

| account | | matches checked | calls | points | quota pauses | wall clock |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| razor#d4c | repeat | 10 of 41 | 4 | 6 | 0 | 157s |
| aliii#2008 | repeat | 30 of 93 | 3 | 4 | 0 | 184s |
| yrn#wkmts | repeat | 44 of 45 | 3 | 4 | 0 | 164s |
| muddy#mud1 | new, NA | 36 | 30 | 55 | 1 | 466s |
| d3mur#ari | new, EU | 19 | 13 | 23 | 0 | 319s |
| ZETA Hater#0712 | new, AP | 40 | 34 | 61 | 2 | 463s |
| blackcatw#Hartz | new, KR | 30 | 24 | 43 | 1 | 141s |
| resenha master#pollo | new, BR | 16 | 10 | 17 | 0 | 155s |

**The HenrikDev side is finished.** A repeat scan is three or four calls and
never waits for a window. razor's run skipped 31 of its 41 matches as already
settled and bought three of its ten new ones in a single call. Even a cold
account on the far side of the world costs at most 61 points, which is two
minutes of a Basic key.

**The time is now entirely Twitch and OCR, and it does not scale with matches.**
razor checked ten matches in 157s; yrn checked forty-four in 164s. The floor is
the per-match sweep: every match pulls every Valorant clip created worldwide in
its window and reads a bio for each channel behind them - 1,206 bios for razor's
ten matches, 5,357 for yrn's forty-four - before a single frame is read. What
actually moves the number is frames: muddy read 140 and took 466s, blackcatw
read 10 and took 141s.

A hundred matches at yrn's rate is about six minutes here, and seven times that
on the deployed box. Under five minutes is close but not yet true, and the lever
is no longer the API.

#### How often there is anything to find at all

The same eight accounts, counting opportunities rather than answers. "A
candidate" is a channel plausible enough to weigh; "a surviving broadcast" is
one of those with a VOD that still covers the match.

| account | matches | lobby-mates | with a candidate | with a surviving broadcast | confirmed | likely |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| razor#d4c | 40 | 261 | 197 | 7 | 0 | 0 |
| aliii#2008 | 93 | 651 | 531 | 23 | 1 | 3 |
| yrn#wkmts | 45 | 334 | 215 | **6** | 0 | 0 |
| muddy#mud1 | 36 | 251 | 192 | 12 | 0 | 3 |
| d3mur#ari | 19 | 130 | 92 | 6 | 2 | 1 |
| ZETA Hater#0712 | 40 | 260 | 168 | 8 | 0 | 0 |
| blackcatw#Hartz | 30 | 222 | 102 | 2 | 0 | 0 |
| resenha master#pollo | 16 | 118 | 85 | 3 | 0 | 1 |
| **total** | **319** | **2,227** | **1,582 (71%)** | **67 (3.0%)** | **3** | **8** |

Seventy-one per cent of the people you play with have *something* that looks
like a Twitch channel, and three per cent have one whose broadcast of that match
still exists. Sixty-seven openings across three hundred and nineteen matches,
and eleven of them turned into a find.

This is the answer to "a Radiant player must have met somebody". `yrn#wkmts` had
**six** openings in a fortnight. At the rate the other seven accounts convert
them, six openings are worth about one find, and drawing zero from six is an
ordinary afternoon. The ladder is not the constraint and neither is the name
matching; Twitch deleting broadcasts is.

Worth noting what razor's row does *not* say. It shows zero confirmed in the
window while the page shows two, because vaerie and cocapuppy are on matches
that have since aged out of fourteen days. That is the exact gap the "not
re-checked for N days" line on each card now exists to explain.

#### The bios we had already read (2026-11-21)

With the frames reordered, the rest of a scan's time is Twitch, and most of
that is one call. Layer 4 asks `/helix/clips` for every Valorant clip created
during the match -- a pool of roughly 170 channels -- and then reads all of
their bios through `/helix/users`, looking for a `NAME#TAG` belonging to
somebody in the lobby. A sixteen-match scan of `d3mur#ari` read **2,805 bios**
doing it.

That pool is not random. It is whoever was being clipped on Valorant in those
minutes, which is the same few hundred channels with an audience, match after
match and scan after scan. Their bios were already in `twitch_channel` from the
first time, and `schema.sql` has had a `description` column since Phase 2.
Nothing read it.

Measured against four real matches of an account already in the database:

| | before | after |
|---|---:|---:|
| clip-pool channels | 684 | 684 |
| bios fetched from Twitch | 684 | **0** |
| `/helix/users` calls | 9 | **0** |
| seconds spent on them | 0.9 | **0.001** |
| Riot IDs found in bios | 1 | 1 |

684 of 684 already on disk, because that database was built by scanning that
account. The reach is unchanged: the one bio that named a lobby member still
names them, read from SQLite instead of fetched again.

The general case is weaker than 100% and was measured too. Across ten
databases, 35,039 of 46,771 stored channels carry a bio -- **74.9%** -- and
every one of them is inside the fortnight the cache trusts. The fortnight is
Twitch's own VOD retention for a channel without a subscription, so a bio too
old to use could only ever have mattered to a match whose broadcast is already
gone. The one thing a cached bio can cost is a streamer who added their Riot ID
to their bio since we last looked, and fourteen days is the bound on that.

A brand-new account still pays in full on its first scan, because every account
gets its own database. It need not: the other nine databases between them
already hold **64.1%** of d3mur's bios. A shared `twitch_channel` table would
hand a new account two thirds of its bio lookups before it starts, which is
the strongest argument yet for the shared-table item in
[OPEN.md](OPEN.md).

#### The layer that has never once fired

Worth saying plainly, since the above makes it cheap rather than useless.
Across nineteen databases and 16,923 recorded encounters, the number ever found
by `clip_window+bio` -- a Riot ID read out of a clip-pool channel's bio -- is
**zero**. Every clip-derived encounter on disk came from the name-prefix
fallback instead:

| how it was found | encounters | confirmed or likely |
|---|---:|---:|
| `name_prefix` | 12,484 | 5 |
| `name_match` | 4,410 | 10 |
| `clip_window` | 17 | 7 |
| `alias:clip_window` | 7 | 5 |
| `alias:name_prefix` | 2 | 2 |
| `alias:name_match` | 2 | 2 |
| `clip_window+bio` | **0** | 0 |

That is not an argument for deleting it. The bio is the only half of Layer 4
that can reach a channel whose name says nothing about the Riot ID, which is
the case the whole layer exists for; without it, Layer 4 collapses into "a
name-resembling channel that was also being clipped". But it does mean the
cost was buying a possibility rather than a result, and now it costs almost
nothing.

### How a streamer is confirmed, and the two ways to get it wrong

Finding the channel is the easy half. Proving it is the right person is the whole
problem, and it has two failure modes that pull in opposite directions.

**Confirming someone who was not there.** The natural test — does this channel have a
broadcast spanning my match? — is worth almost nothing alone. It asks whether somebody was
*broadcasting*, never what was on screen, and a channel that streams daily passes it for any
match ever played. A lobby-mate called `Curry#2622` was duly matched to `twitch.tv/curry`, a
radiant Valorant streamer, on an exact name match plus a covering broadcast. Wrong person:
the real Curry was a friend with no channel, and that broadcast was titled *SECRET DEADLOCK
STREAM*.

**Missing someone who was.** The fix for the above was to demand hard evidence — a Valorant
clip cut from the covering VOD inside the match window. That quietly introduced the opposite
bug, because *only streamers with an audience ever get clipped*. `CocaPup#slay` was streaming
the whole of a Sunset match from the enemy team, on `twitch.tv/cocapuppy`, under the title
*"short valo stream!! !nt !lurk"*, with a bio reading *"WELCOME!!! I am CocaPup!!"* — and we
reported that match as empty. Two rules hid them: Layer 1 demanded an *exact* name match, so
`cocapup` != `cocapuppy` meant no candidate was ever created; and confirmation demanded a clip
that a self-described new streamer will never have.

So candidate generation is deliberately **loose** and confirmation is **strict**. That split
is the design. Generation casts wide — exact names plus prefix near-misses, capped at six per
player because a four-character Riot ID like `Nuke` resembles hundreds of channels. Then a
candidate is confirmed only by:

- **the lobby's own names, read off the broadcast** - four or more of the *other* players in
  the match appearing on screen in that VOD. Decisive, independent of channel size, and the
  only signal that can also rule a candidate *out*. See *Reading the broadcast* below; or
- **a Riot ID the channel advertises**, name and tagline both agreeing.

That is the whole list, and it used to be longer. Everything else — a name that resembles
another name, a bio naming the player in plain words, a broadcast that was Valorant, a clip
cut from it during the match — reaches **likely**, which the page shows and the alias table
refuses.

Ruled out when we read the broadcast and the match was not in it, when the broadcast's clips
are all some other game, when its title names another game, when every advertised Riot ID has
a different tagline, or when no broadcast holds at least 60% of the match.

### The clip test proved the wrong thing

A Valorant clip cut from the covering VOD at an offset inside the match used to confirm a
candidate outright. The argument was that clips carry `video_id`, `vod_offset` and `game_id`,
which is the one way to reconstruct what a past broadcast contained — `GET /helix/videos`
carries no game id and `GET /helix/streams` only answers for channels live right now.

All of that is true and none of it is about the match. The clip says Valorant was on screen
during those minutes. For a channel that streams Valorant daily, that is true of every match
its broadcast happens to overlap, so it cannot tell the player from a stranger. It is the
`curry` fallacy with one extra step: curry was ruled out because the clips were Deadlock, and
a Valorant streamer passes the same test every time.

Reported by a user of `yrn#wkmts`, and then measured. Two confirmed streamers, neither of
whom was in the match:

| | |
|---|---|
| Channel | `twitch.tv/NeonQuin`, confirmed as `NeoN#Rage` |
| Match | Lotus, 18 November 2026, 17:02:22 UTC |
| Evidence that confirmed it | one Valorant clip from the covering VOD, at offset 5979 |
| The frame the page linked to | VOD `2932423001` at 1:34:34 — **ROUND 3, score 0-2** |
| What the page said was there | round 11 of a 13-6 match |
| The five agents on screen | none of the five in our match |

`twitch.tv/NextLuna` was confirmed as `next#xnte` in a Haven match on 6 November the same
way. Both ties came from the same second defect: the clip pool matched a channel to a player
on `_prefix_related`'s **three-character default**, which Layer 1 overrides to five and
Layer 4 did not. `NeoN` -> `neonquin` and `next` -> `nextluna` are four letters of a shared
prefix between strangers. Layer 4 now uses the same five-character bar as Layer 1.

Layer 5 had already looked at NeonQuin's broadcast and read ten frames of it, five of them
unmistakably Valorant, with **zero of the eight readable lobby names**. The code called that
inconclusive — "maybe an overlay, maybe the streamer was in a menu" — and the clip test
confirmed the channel anyway, two checks further down. Both halves are fixed: Valorant on
screen with none of this lobby in it is now a positive reading of *somebody else's match*,
and it is weighed before anything circumstantial.

The cost of being wrong here is not one bad row. A confirmation writes a permanent alias,
Layer 2 returns it first on every later match, and the mistake comes back looking like
history repeating as corroboration. `restate_verdicts` re-judges every stored encounter from
the evidence already on the row — no API call, no frame — which is the only way a match whose
broadcasts Twitch has deleted ever gets corrected. Run against this repository's own
`razor#d4c` database, it left both real finds (`vaerie`, `cocapuppy`) untouched.

Two details that carry more weight than they look like they should. The **broadcast title** is
a free game check — *"short valo stream"* against *"SECRET DEADLOCK STREAM"* — and a
watchparty or react title counts against, because watching VCT is not playing it. And a bare
name in a **bio** is matched only as a whole word after emails, URLs and bare domains are
stripped; without that, `curry@proxy.gg` and `curry.gg` re-confirm the wrong curry.

Everything below confirmed prints as **unverified**, under a heading saying so, and is never
written to the alias table — the alias table is consulted first on every later match, so one
wrong row is permanent and comes back looking like corroboration.

A VOD is chosen by **seconds of overlap**, not pass/fail coverage. A stream picked up two
minutes into a match still holds every later round; the link maths refuses to point at a
moment before the stream began rather than clamping to `t=0`.

### Reading the broadcast

Everything above this point is circumstantial. A name that matches, a title that says
valorant, a stream that was running at the right minute: all of it is consistent with the
right person and equally consistent with a stranger who happened to be live. The only thing
that settles it is opening the video and looking, which is what klips.live means when it says
it confirms the streamer from their saved video.

Valorant paints the lobby's names on screen constantly - the killfeed, the combat report in
the corner, the round-end scoreboard, the *switch player* banner while you are dead and
spectating. The match history already gives us all ten names and the millisecond of every
kill. So the test is direct: seek to a moment when something was happening, read the text, and
count how many of those ten names come back.

`yt-dlp` turns a VOD id into an HLS playlist and `ffmpeg` seeks it for a single frame. Seeking
a Twitch DVR playlist is cheap - a frame from eight hours into a broadcast costs about two
seconds, not an eight-hour download - and frames are cached, so re-running a sweep re-reads
nothing.

Which seconds to sample matters more than how many. Round ends are tried first, because the
scoreboard shows all ten names at once; kills come second, where the killfeed shows two.
Sampling stops the moment the answer is certain.

Measured on the four broadcasts where the answer was already known by other means:

| broadcast | what was actually on screen | lobby names read |
| --- | --- | --- |
| `twitch.tv/vaerie` | our Corrode match | **9** of 10, on the first frame |
| `twitch.tv/cocapuppy` | our Sunset match | **10** of 10, on the first frame |
| `twitch.tv/solly17x` | Resident Evil | 0, over 10 frames |
| `twitch.tv/curry` | Deadlock | 0, over 10 frames |

There is no grey zone between nine and zero to tune a threshold into, which is the nicest
property a signal can have. Four distinct names is the bar, counted after the channel's own
name is discarded (see *Checking for the streamers we missed*). Two things make a small number
decisive: the broadcast already overlaps this match's wall-clock, and at that moment those ten
people were in that match - so seeing four of them means the broadcaster was in it too.

It cuts both ways, and that is the point. `Solly#Busta` matched `twitch.tv/solly17x` on a
prefix, with a VOD covering the whole match and a title - *"finishing it."* - that gave
nothing away either direction. It sat as **unverified** because nothing could decide it. It
was a Resident Evil campaign. This is the only layer that can rule a candidate out on evidence
rather than on the absence of it, so it outranks every circumstantial signal beneath it: a
broadcast we have read and found the match missing from is rejected even when the name, the
bio and the title all agree.

One honest limitation. A match timer at the top of the screen is *not* Valorant's alone -
Deadlock draws one within pixels of the same place - so a timer never counts as evidence of
Valorant by itself. Only Valorant's own furniture does: *COMBAT REPORT*, *BUY PHASE*, *ROUND
ONGOING*, *SWITCH PLAYER*.

This layer is optional. It needs `yt-dlp` and `ffmpeg` on `PATH` plus
`pip install 'clipsvalorant[vision]'`; without any of them every other layer works unchanged
and the resolver reports what is missing rather than failing. Turn it off with `--no-watch`.

### Measured across the rank ladder

Run over nine accounts spanning the NA top-500 ladder plus one ordinary account,
43 matches in all, 1,500 candidates generated:

| account | ladder rank | matches | confirmed |
| --- | ---: | ---: | --- |
| MIBR zekken | 1 | 5 | `yay -> yayster` (x2) |
| twitch stunna | 3 | 5 | - |
| enzo | 10 | 5 | `KILLERJOHNSON77 -> killerjohnson77` |
| dont speak to me | 60 | 5 | `Redefy -> redefy`, `punz -> punz` |
| aika | 150 | 5 | - |
| spaceuk | 250 | 5 | `Subroza -> subroza`, `reduxx -> reduxxval` |
| Hayden Patriquin | 350 | 5 | `Marved -> marved6` |
| houu | 480 | 5 | - |
| Xeppaa | 6 | 3 | `GeneralSteak -> generalsteakt` |

Twelve confirmations in 43 matches, against two in the 44 matches of an ordinary
account. The difference is not the detector; it is who you are matched against.
High-elo lobbies are dense with people who stream for a living, and the names
coming back - yay, Subroza, Marved, punz - are exactly the ones you would expect.

**Speed.** Between 28 and 152 seconds for five matches, averaging about 19
seconds per match end to end, of which ingest is 3-4 seconds and the rest is
Twitch search. A fresh 20-match history lands in six or seven minutes. Reading
broadcasts barely registers: only VODs that survived are opened, sampling stops
the moment it is certain, and 169 frames covered all 43 matches. One confirmation
took a single frame.

### Checking for the streamers we missed

A false positive announces itself: you click a link and a stranger is playing
Deadlock. A false negative is silent - a streamer we quietly binned looks exactly
like a match where nobody was streaming. So the two need very different checks,
and only one of them can be done by reading the output.

Sorting every rejection in a 44-match database by *why* it was rejected:

| reason | count |
| --- | ---: |
| no broadcast of that match survived at all | 1,775 |
| the title named another game | 4 |
| every clip from it was another game | 3 |
| we read the broadcast and the match was not in it | 1 |

So 99.6% of rejections are not judgements. There is no video; nothing was weighed
and nothing could have been decided differently. Only **seven** were decisions,
which is the entire surface a missed streamer could be hiding behind. `audit`
walks exactly that surface, re-reading each one with twice the usual frame budget:

```bash
python -m clipsvalorant audit --limit 200 --frames 20
```

On the database above it overturned nothing. What it did turn up was more
interesting than a flipped verdict.

**Every non-Valorant broadcast it re-read returned exactly one "lobby name".**
`twitch.tv/curry` showed CURRY, `twitch.tv/rogue` showed Rogue, `sacred` showed
their Apex in-game name `sacred_NRG`. None is a coincidence: a channel is only a
candidate *because* its name resembles the player's, so its own watermark, overlay
and title card name something very like that player by construction. It is the
candidate agreeing with itself.

That is now excluded. When reading a broadcast for player P, P's own name does not
count - only the other nine, about whom nothing on that channel predicts anything.
Without the exclusion every name-matched channel with a surviving VOD sat one weak
signal away from confirmation; with it, the evidence is independent of the reason
we looked. It costs nothing in recall: the broadcast that returned all ten lobby
names still returns nine.

The remaining false-negative surface is almost entirely **candidate generation** -
channels we never thought to look at, because the name resembles nothing and
nobody clipped them. Reading a broadcast cannot help with a broadcast we never
open. That is the next thing worth building, not a better verdict rule.

### The false negatives this is open to, which nobody has measured yet

Open problem, written down rather than acted on. Nothing below has been
measured; it is the case for measuring it.

The count above - seven rejections in a 44-match database that were actually
judgements, 99.6% being "no video survived" - was taken before a Valorant
broadcast with none of the lobby in it became a *rejecting* signal. That change
is right on its own evidence, and it is also the first rule in this project that
can throw away a real streamer on the strength of ten frames. The judgement
surface has grown and has not been re-counted.

What prompts this is a base rate that does not look believable. `aliii#2008`
carried five finds across a hundred matches, and a player with far fewer matches
carried two. `yrn#wkmts` is Radiant, which is the smallest population on the
ladder and the one most densely packed with people who stream, and after the
fixes above the account resolves to nothing at all. One account proves nothing,
but "a Radiant player met no streamers in a fortnight" is a claim that should
have to earn itself.

Four reasons a rejection here could be wrong, none of them yet ruled out:

**One VOD stands in for a channel.** A candidate is weighed against the single
broadcast that overlaps the match. A streamer who went live twice that day has
another, and "this VOD is not the match" says nothing whatever about the other
one. The code does not currently know the difference between *this broadcast*
and *this channel*.

**Variety streamers switch games inside one broadcast.** Ten frames sampled from
one stretch of a six-hour stream describe that stretch. Rejecting a channel
because the frames landed in the hour they played something else is the same
mistake as the clip test, run backwards.

**The sampling and the shift are circular.** Frames are read at an offset
computed from the shift, and the shift is measured from frames. Get it wrong and
you look in the wrong place, see somebody else's match, and conclude you have the
wrong person - which is exactly the shape of the right answer. Dating a frame by
the ROUND banner breaks the circle, but only on frames where the banner is
legible, and nothing yet reports how often that is.

**Ten frames of a six-hour VOD is a twentieth of one percent of it.** That is
ample for confirming - one frame with four lobby names settles it - and it is
very thin for denying. Confirmation and denial are being held to the same
evidence, and they are not symmetrical claims.

What has to be measured first, in this order, before any rule changes:

1. The base rate. Of the players in `yrn#wkmts`'s lobbies, how many have a Twitch
   channel at all? Until that number exists there is no way to tell an
   implausible zero from an ordinary one.
2. Re-sort the rejections by reason on a database scanned under the current
   rules, and see how far "we read it and it was somebody else's match" has
   risen from one.
3. For the two channels this rejected by name, whether any VOD of theirs holds
   the match *anywhere*, read by hand. That settles whether this is a sampling
   failure or a correct answer.

And the constraint that makes it hard: the obvious fix - more frames, more VODs,
more channels - is exactly what the budget in CLAUDE.md forbids. Whatever this
turns into has to buy its evidence more cheaply, not buy more of it. Note that
the clip test, now demoted to "likely", still puts its candidate on the page: a
demotion is visible, where a rejection is silent, and it is the silent one that
needs the care.

#### Reading the rejections by hand (2026-11-21)

`yrn#wkmts` had six candidates whose broadcast still existed. The scan reads
ten frames of each, aimed at the moments that matter. This read them again at
a three-minute grid across the whole broadcast, plus finely inside the match,
using the same name matcher - so "none of that lobby was on screen" had a real
chance to be wrong.

**The rejection that was checked hardest was right.** `twitch.tv/NeonQuin`,
5.9 hours, 157 frames read, **42 of them plainly Valorant, and not one lobby
name anywhere in the broadcast**. They were playing Valorant for most of that
stream and never played *that* match. The ten frames the scan read reached the
same answer as a hundred and fifty-seven.

**And the one that looked like a miss was a collision.** `twitch.tv/nika` was
the best false-negative candidate in the database: the scan saw one lobby name,
`shadow 斬`, which is three short of the bar. Reading 164 frames across 6.25
hours found that name twelve times - and **zero Valorant frames in the entire
broadcast**. The frames say why:

```
smite2 home store profile gods season events level272
izanamishadowstalker emberwarden ... kumbhakarnaracetoradiant
```

nika was playing **SMITE 2** for six hours. The "lobby name" was `shadow`,
which is what `shadow 斬` normalises to once the CJK character is stripped,
sitting inside the skin name *Izanami Shadowstalker*. Not a missed streamer: a
six-letter English word matching inside an unrelated one.

That is a real mechanism and it is not rare in principle. Of the 2,460 distinct
lobby names long enough to search for across these databases, **978 (40%)
normalise to six characters or fewer**, and even a ninety-word sample list of
ordinary English words catches twenty-two of them — `ghost`, `snake`, `water`,
`raven`, `neon`, `shadow`. What stops it mattering is `CERTAIN_NAMES = 4`: one
collision buys "unconfirmed", which is exactly what nika got.

All six, after a first pass that had to be thrown away. That pass walked each
VOD from its start and gave up after eight dead frames, which on a fourteen-
and a nineteen-hour broadcast never reached a match sitting eleven to thirteen
hours in - so "0 frames read" was a fact about the sampling, not the broadcast.
Sampling the match window first fixed it. Only one of the three was genuinely
gone from Twitch, checked against the API rather than assumed.

| channel | VOD | frames read | inside the match | Valorant frames | lobby names | verdict held? |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| nika | 6.2h | 164 | 39 | 0 | `shadow`, a SMITE skin | yes, unconfirmed |
| NeonQuin | 5.9h | 157 | 39 | **42** | none | yes, rejected |
| bayonet888 | 5.0h | 81 | 21 | 0 | none | yes, rejected |
| whiskeyburt | 14.8h | 209 | 31 | 0 | none | yes, rejected |
| speed_gunner | 19.4h | 273 | 40 | 0 | `mell`, outside the match | yes, rejected |
| devastated_offf | 1.3h | — | — | — | — | VOD deleted since |

**884 frames across roughly fifty-one hours of broadcast, and not one lobby
member on screen during any of those matches.** Every verdict held.

Four of the five were not even Valorant streams. `bayon` → bayonet888,
`Whiskey` → whiskeyburt, `SPEED` → speed_gunner and `NIKA` → nika are all
Layer 1 name resemblances, and none of those channels had Valorant on screen in
any frame sampled. Layer 5 ruled out exactly what it should have. The only
candidate who really was playing Valorant was the only one it had to think
about.

#### The guard that would have broken it

The obvious fix is to count a lobby name only on a frame that carries
Valorant's own furniture. nika's 164 frames had none. Measured against every
confirmed or likely encounter in every database before changing anything:

| | |
| --- | ---: |
| encounters re-read | 31 |
| name sightings on a frame with Valorant UI words | 107 |
| name sightings only ever seen off such a frame | **16** |
| encounters that would lose evidence under a per-frame guard | **8** |
| encounters with no Valorant frame anywhere in the read | **2** |

Both versions of the guard destroy real finds. `twitch.tv/henkyyyval` and
`twitch.tv/kazuhira323` are confirmed on four and three lobby names and have
**no Valorant UI word on any of their ten frames**. Here is the frame that
confirms henkyyyval:

```
info  02  aktivujtewindows  d3mur shaaw myagi1x  prejdtedonastavenfaaktivujtesystemwindows
```

Three lobby members, a round number, and a Slovak Windows activation watermark.
It is overwhelming evidence and it carries none of the fifteen words in
`VALORANT_UI_WORDS`, because that list is gameplay-HUD vocabulary and this is
not a gameplay HUD. So `looks_like_valorant` is a narrow detector that misses
real Valorant screens, and gating name evidence on it would have turned two
confirmations into silence - the precise failure this section exists to find.

**Nothing was changed.** Three names on one frame is stronger evidence than any
UI word, the bar that protects against collisions is the count of distinct
names and it held, and the only rule change available was measured to cost more
than it saved.

#### The one frame that shows the whole lobby (2026-11-21)

A killfeed frame carries two names and four are needed, which is why a
candidate costs ten frames. Two screens carry all ten at once: the load-in
screen, which lists both teams with their agents and the map, and the post-game
scoreboard. Both sit at positions arithmetic already knows - the match's start,
and its start plus its length - so the appealing version of this idea is to
replace ten sampled frames with one computed one.

Measured over the 31 confirmed and likely encounters, aiming with the median
shift the way a real scan must:

| what was aimed at | settled |
| --- | ---: |
| one frame at the match start | 2/31 |
| one frame 25s after the match ends | 4/31 |
| both | 6/31 |
| all ten positions | 14/31 |

So the one-frame version does not work, and **the reason is the clock, not the
screen**. Where a probe happened to land on the true match start the load-in
screen is right there and complete - `twitch.tv/vaerie` returns **nine of nine
names off a single frame**. But the gap between `started_at` and the in-match
clock, measured across seventeen broadcasts, runs from **75.8s to 210.2s**, and
`calibrate.py` was written against three that happened to fall between 101 and
125. Aiming with a median that can be half a minute wrong cannot reliably hit a
screen that is up for twenty seconds.

Re-aimed with each broadcast's own measured shift, the best single position
still settles only 5 of 17, and the frames say why: **agent select hides the
enemy team**, so any frame before the match has a ceiling of four names, and
several streamers were alt-tabbed into a tracker overlay. The pre-match anchor
has a ceiling the post-game one does not.

What survived is the post-game scoreboard, and not as a replacement. The
killfeed and the scoreboard are complementary: `twitch.tv/punz` caps at three
names across ten killfeed frames and gets five off one scoreboard, while
`twitch.tv/killerjohnson77` has three names by its second killfeed frame, its
fourth not until its ninth, and nothing on any scoreboard frame. So the
scoreboard is tried after three killfeed frames, and only when fewer than three
names are already in hand.

Run through the shipped `watch` both ways, same machine:

| | settled | frames |
| --- | ---: | ---: |
| confirmed and likely, before | 14/31 | 208 |
| confirmed and likely, after | **17/31** | **198** |
| rejected, before | 0/89 | 765 |
| rejected, after | 0/89 | 767 |

Three gained, none lost, ten frames saved where it matters and two spent where
it does not. `twitch.tv/redefy` went from three names in ten frames to **nine
in seven**. The rejections are flat by construction: the anchors displace
killfeed frames rather than adding to them, so a candidate that is the wrong
person costs what it always did, which is the case that decides whether a scan
got slower.

The collision risk was measured before shipping, because a scoreboard puts ten
names on screen where a killfeed puts two, and a lobby name is matched as a
substring. Across **272 anchor frames on 73 rejections, 71 returned no lobby
name at all and two returned two**. None reached four.

### Measuring recall, and the layer that was not built

Knowing what the tool claims is easy; knowing what it misses is not. A missed
streamer is indistinguishable from a match where nobody streamed, so the only way
to find out is to build a second detector that fails differently and see what it
turns up.

Layer 4 already fetches every Valorant clip made during a match, which names the
channels demonstrably live on Valorant at that moment. It then keeps only those
whose name resembles a lobby member or whose bio advertises a Riot ID. The obvious
next layer is to stop filtering: read *every* covering broadcast and keep whichever
show four or more lobby names, regardless of what they are called.

That is affordable, which was itself a surprise. A clip names the VOD it came from,
and `/helix/videos` takes a hundred video ids per call, so a whole match's pool of
broadcasts costs one request instead of one per channel - 89 VODs in a single call
where the naive form made 114. A single frame at a round end shows the entire
scoreboard, so one frame decides nearly every VOD, and frame fetching parallelises
about 3.6x. A match goes from sixteen minutes to roughly ninety seconds.

So it was built as a measurement, run over ten matches, reading about 1,100 covering
broadcasts exhaustively:

| | |
| --- | ---: |
| streamers it found that the name-based detector missed | **0** |
| streamers the name-based detector found that it missed | **2** |

It never once beat the existing funnel, and it was blind twice: `cocapuppy`, whose
stream was titled *"short valo stream!! !nt !lurk"*, and `redefy` - neither of whom
anybody had clipped, so neither appeared in the pool at all. That is the same
audience bias that made clip evidence alone miss small streamers in the first place.

The two approaches have complementary blind spots, and the exhaustive one is the
weaker: it cannot see a streamer nobody clips, while names reach anyone. So the
layer was measured and then not built. The code that made it affordable
(`videos_by_id`) is kept, because it is the cheap way to ask about many broadcasts
at once.

One honest limit remains: none of this yields an *absolute* recall figure, and no
amount of effort will. Twitch has no endpoint that answers "who was broadcasting
this game at this time" - `/helix/streams` is live-only, `/helix/videos` needs a
user id, and clips only surface channels somebody clipped. Every possible oracle is
itself a biased sample. What can be said is that the best available independent
check, run exhaustively, found nothing the funnel had missed.

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

### Running it on a schedule

Everything above is about identifying people correctly. This is about looking in
time, and on the evidence it matters more. 1,775 of 1,783 rejections were "no
broadcast survived", so the deciding factor in whether a streamer is ever found is
*when* you look, not how cleverly.

```bash
python -m clipsvalorant sweep --dry-run   # what it would do, fetching nothing
python -m clipsvalorant sweep --quiet     # a pass; prints only when it finds somebody
```

A sweep ingests new matches and then resolves only what is worth resolving:

- **older than 14 days** - never. Twitch has deleted the broadcasts; there is
  nothing left to find and asking costs a call per player.
- **never looked at** - always.
- **young, unsettled, last looked at a while ago** - again, up to five times. A VOD
  is published when the broadcast *ends*, which can be hours after the match, and
  viewers keep cutting clips from it for days. The evidence that confirms a streamer
  mostly arrives after the match does, so a single look at ingest time is not enough.
- **already confirmed, or out of attempts** - no. Stop spending quota on a settled
  question.
- **no highlights of your own** - no. Finding a streamer there would produce nothing
  to link to.

`--quiet` prints only when something was confirmed, which is what you want from
anything that mails you its output. Run it a few times a day; there is no value in
running it more often than VODs appear, and none at all in running it on matches
past retention.

For a match that has only just ended the broadcast may still be running, and
`/helix/streams` will say what the channel is playing. That is the one authoritative
game check available, and the only one a title cannot lie to - one broadcast in this
sample was titled *VCT OQ Watchparty* while showing Deadlock. It is corroboration,
never a confirmation: being live on Valorant now says nothing about who was in a
lobby an hour ago.

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
