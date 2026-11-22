# Open questions

Things known to be unfinished. Each one says what has already been measured and
what would settle it, because the failure mode this project keeps repeating is a
confident answer that nobody checked. Nothing here is a plan. It is the list of
questions that have earned the right to be asked.

The reasoning lives in [README.md](README.md), the architecture in
[PLAN.md](PLAN.md), the traps and the budget in [CLAUDE.md](CLAUDE.md).

---

## 1. What a rejection is worth

The long form is *The false negatives this is open to* in the README. The short
form: a false positive announces itself and a false negative is silent, and the
rules were recently given a new way to reject somebody.

**Measured 2026-11-21**, every stored rejection across all fourteen local
databases, re-sorted under the rules in force now:

| reason | count | a judgement? |
| --- | ---: | --- |
| no broadcast of that match survived at all | 9,674 | no |
| read it: none of the lobby was on screen | 33 | yes |
| the broadcast was there but could not be read | 9 | no |
| every clip from it was another game | 6 | yes |
| read it: somebody else's Valorant match | 1 | yes |
| no signal either way | 1 | no |
| **total** | **9,724** | **40 judged (0.41%)** |

So the verdict rules are not where the misses are. Two in five hundred
rejections were decisions at all; the rest is Twitch deleting broadcasts. The
new rejecting signal — a Valorant stream with none of the lobby in it — has
fired exactly **once** in everything this project has ever scanned. That is
reassuring about the rule and useless as a test of it.

**And a finding that reframes the whole worry.** `yrn#wkmts` resolving to
nothing was never a verdict. Forty-four of its forty-five in-window matches have
`resolve_attempts = 0`: ingested, never resolved, never looked at. Its sixty-three
rejections all belong to the single match that was. "A Radiant player met no
streamers in a fortnight" was not a claim the scanner ever made.

**The base rate, measured 2026-11-21** across eight accounts and 319 matches
(the full table is in the README): of 2,227 lobby-mates, 1,582 produced a
candidate worth weighing and **67 — three per cent — had a candidate whose
broadcast still existed**. Those 67 openings produced 3 confirmed and 8 likely.

`yrn#wkmts` had **six** openings in a fortnight. At the rate the other seven
accounts convert them that is worth about one find, so drawing zero is an
ordinary afternoon rather than evidence of anything. The question "shouldn't a
Radiant player meet somebody" is answered, and the answer is that Twitch
retention, not rank and not name matching, sets the ceiling.

**Those six broadcasts have now been read end to end** (the long form is
*Reading the rejections by hand* in the README), and no false negative came out
of it. `twitch.tv/NeonQuin` gave 157 frames, 42 of them plainly Valorant, and
not one lobby name in 5.9 hours: the rejection was right and the scan's ten
frames reached the same answer. `twitch.tv/nika`, the best candidate for a miss
in the whole database, was playing SMITE 2 for six hours - its single "lobby
name" was `shadow`, the normalised form of `shadow 斬`, matching inside the skin
name *Izanami Shadowstalker*.

What that turned up instead is a collision risk on the *confirming* side: 978
of the 2,460 searchable lobby names (40%) normalise to six characters or fewer,
and plenty of those are ordinary words. `CERTAIN_NAMES = 4` is what keeps it
harmless, and it held - nika scored one and stayed unconfirmed.

The obvious guard, counting a name only on a frame carrying Valorant UI words,
was measured against all 31 confirmed or likely encounters before being written:
it would strip evidence from 8 of them and destroy 2 confirmations outright,
because `looks_like_valorant` misses real Valorant screens. Nothing was changed.

Still open, and smaller than it looked:

- `looks_like_valorant` is a narrow detector. It reads False on a frame showing
  three lobby members and a round number, because its fifteen words are
  gameplay-HUD vocabulary. Anything that gates on it inherits that blind spot -
  including the `other_valorant` rejection, which needs three Valorant frames
  before it fires and will therefore simply not fire rather than fire wrongly.
  Conservative in the right direction, but it should be known.
- Whether a name that normalises to an ordinary word should need more than one
  sighting. The measurement above says the current bar already handles it; this
  is only worth revisiting if a false confirmation ever shows up.

---

## 2. The backend: does it do what it is meant to, and what else should it hold?

**What exists.** One SQLite database per account under `data/`, named for the
Riot ID. `match`, `match_player`, `kill`, `round` and `highlight` are the match;
`twitch_channel`, `twitch_vod`, `alias`, `encounter` and `match_vod_offset` are
the identity work; `frames/` beside them caches stills pulled out of VODs. The
"do not pay twice" policy is `sweep.matches_due`: nothing older than fourteen
days, a re-check of anything under four days old every twenty hours, and five
attempts before a match is left alone.

**Measured 2026-11-21**, what a repeat scan would actually do:

| account | in window | due | settled |
| --- | ---: | ---: | ---: |
| razor#d4c | 38 | 7 | 31 |
| aliii#2008 | 93 | 30 | 63 |
| yrn#wkmts | 45 | 44 | 1 |

The policy works. razor's seven are the matches under four days old, which are
re-checked because a Twitch VOD can appear after the match it contains. yrn's
forty-four are due because nothing ever resolved them, which is question 1 and
not this one.

**The question this raises.** Splitting by account makes every account pay again
for the same Twitch lookups. Across the fourteen databases there are **29,536
channel rows but only 19,065 distinct ones — 10,471 duplicate lookups, 35% of
everything stored**, with three channels appearing in eleven of the fourteen.
VODs duplicate less (9%), because a VOD only matters inside one window.

Worth answering, in roughly this order:

- Should `twitch_channel`, `twitch_vod` and `alias` move to one shared database
  while `match` and `encounter` stay per account? The duplicate rate says the
  saving is real; what it costs in write contention with several scans running
  has not been looked at, and `WORKERS = 1` means there is only ever one writer
  today.

  **This now has a price on it.** Bios are read off disk when they are already
  there, which on a re-scan of a known account answers the whole clip pool -
  684 of 684 channels across four real matches, nine `/helix/users` calls down
  to none. A *new* account gets none of that, because its database is empty.
  But the other nine databases between them already hold **64.1%** of
  `d3mur#ari`'s bios, so a shared channel table would hand a first-time scan
  two thirds of its bio lookups before it starts. That is the whole remaining
  benefit of this item on the speed side, and it is now a measured number
  rather than a guess.
- **Can the VODs themselves be kept?** Right now the segments are fetched,
  one frame is taken, and the rest is thrown away. Keeping even a few seconds
  around each moment that matters would make a re-check free, and would make the
  false-negative work in question 1 possible without re-downloading anything
  Twitch may have deleted by then. Measure the disk first: this box has very
  little of it.
- Does the frame cache actually survive a deploy? `app_data:/data` is a named
  Docker volume so it should, but that has never been checked after a real
  restart, and the answer changes what a deploy costs.
- Should stored evidence carry the version of the rules that judged it?
  `restate_verdicts` re-judges every row on every scan because it has no way to
  know which rows are already current. It is cheap, but it is cheap by accident.

---

## 3. Is the Oracle box still the right place for this?

**What the box is.** An Always Free VM: 1/8 OCPU and 952 MB of RAM. One match
costs about 34s there against about 4.7s on a 12-core laptop. That 7× is the
single biggest number in this project and it has caused two wrong diagnoses
already.

**What changed.** A repeat scan is now three or four HenrikDev calls and never
waits for a quota window. **Measured 2026-11-21 across eight accounts**, the
API is no longer where the time goes: razor#d4c checked ten matches in 157s and
yrn#wkmts checked forty-four in 164s, because the floor is the per-match Twitch
sweep - thousands of bios read before a frame is touched - and what moves the
number past that floor is VOD frames, not matches.

Those bios are now read from disk when they are already there, which on a
re-scan removes them from the floor entirely. The eight-account numbers above
were measured before that and have not been re-taken, so treat them as the
ceiling rather than the current cost.

A hundred matches at that rate is about **six minutes on this machine**, and the
box is roughly seven times slower. So "under five minutes for a hundred" is
close but not yet true, and it will not be the API that gets it there. The
shared-cache question in section 2 looked like the biggest lever on speed, and
measuring it showed it is not: bios are paid for a hundred at a time, so having
them on disk removes about 1.4% of a scan's requests. The listings are the
lever. Asking "does this channel have a broadcast?" was ~80% of the requests a
scan made and ~88% of the answers were no, which `twitch_vod` could not record
because it only holds a row for a channel that *has* one. Writing the absence
down removed 89% of those requests. See *The question nobody was asking* below.
The branch `claude/hungry-kilby-5aa68d` rewrites this section more fully with
the contention measurement that settled the shared-database question.

**What it needs from a host**, which is what rules most of them out: `ffmpeg`
and `yt-dlp` on PATH, onnxruntime for OCR, a persistent disk for the databases
and the frame cache, a process that can run for minutes without being killed,
and roughly a gigabyte of memory.

- **Vercel is the wrong shape.** Serverless functions are capped well below the
  length of a scan and there is no persistent filesystem, and the entire "never
  re-scan what is already known" rule in CLAUDE.md depends on having one. It
  could host the page; it cannot host the scan.
- **Fly.io is the right shape** — persistent volumes, long-running processes, a
  machine with real CPU — and the open questions are what it costs past the free
  allowance, and whether the scan should become a queued worker rather than
  something that happens inside a request.
- **Staying put is a real answer too.** The box is free, it survives reboots
  unattended, and the work above has made the slow part smaller.

**What would settle it**: run a hundred matches end to end on this machine and
on the box, both under the current code, and put the two numbers side by side.
Everything else is speculation about hardware, which is the thing this project
is worst at guessing.
