# clipsvalorant

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

```bash
python -m clipsvalorant calibrate
```
Measures every confirmed encounter not yet measured. Costs frames, not API calls. New
resolves do it automatically; this is for links found before the module existed.

Where it cannot be measured — no OCR installed, or a stream that joined too late to
show a countdown — the 109s median stands in, and the UI says so on the card rather
than presenting a guess as a measurement.

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

Two things that sound like savings and are not. OCR does not thread — onnxruntime
already uses every core, so running frames in parallel made a pass *slower* (8.9s to
12.9s on six frames). And calibrating from frames the confirmation pass already read
rarely fires, because a streamer confirmed from the screen is usually confirmed on the
first frame, leaving nothing to reuse. It is kept because it costs nothing when it
misses, but it is not where the time went.

What is left is the shape of the problem rather than an inefficiency: klips.live is
fast partly because its alias database is shared across everybody who uses it, so most
lobbies are a lookup rather than a search. This one starts empty and per-account, and
only `alias` rows accumulated from previous runs close that gap.

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
- **a Valorant clip cut from the covering VOD at an offset inside the match** — decisive, and
  the only signal that is independent of channel size. Clips carry `video_id`, `vod_offset`
  and `game_id`, which is the one way to reconstruct what a past broadcast contained, since
  `GET /helix/videos` carries no game id and `GET /helix/streams` only answers for channels
  live right now; or
- **a Riot ID the channel advertises**, name and tagline both agreeing; or
- **three weak signals agreeing**: the channel is named for the player, its bio names them in
  plain words, and the broadcast was Valorant. None of these is worth much alone. Together
  they reach the small streamers that clip evidence structurally cannot.

Ruled out when we read the broadcast and the match was not in it, when the broadcast's clips
are all some other game, when its title names another game, when every advertised Riot ID has
a different tagline, or when no broadcast holds at least 60% of the match.

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
