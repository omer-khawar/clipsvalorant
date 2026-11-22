# Working in this repository

Notes for Claude. Everything here is either a fact about the environment that cannot be
read off the code, or a trap that has already cost a day once. The architecture itself
is in [PLAN.md](PLAN.md), the reasoning and the measurements are in
[README.md](README.md), the deployment is in [deploy/ORACLE.md](deploy/ORACLE.md), and
what is known to be unfinished is in [OPEN.md](OPEN.md) — read that before deciding
something is settled.

## Running things

Windows. The interpreter is in the project venv and `python` on PATH is a different one
that has no dependencies installed:

```bash
.venv/Scripts/python.exe -m pytest -q
```

The whole suite runs in about 25 seconds and needs no network. Run it before every
commit; it is fast enough that there is no excuse not to.

Python 3.11+. Runtime dependencies are `httpx`, `pydantic` and `typer`; Layer 5 adds
`rapidocr-onnxruntime`, `pillow` and `numpy`, plus `yt-dlp` and `ffmpeg` on PATH.

## Deploying

An Oracle Always Free VM runs the deployed copy. `/opt/clipsvalorant` is a real git
checkout, so a deploy is:

```bash
ssh ubuntu@<the box> 'cd /opt/clipsvalorant && git pull && sudo systemctl restart clipsvalorant'
```

About 40 seconds for a Python change, because the Dockerfile installs dependencies
before it copies source. Touching `pyproject.toml` reinstalls onnxruntime and opencv and
costs five minutes. The site answers 502 for a few seconds during a restart while the
app imports onnxruntime; it clears itself. A restart also drops any scan in progress,
because the queue is in memory.

Secrets live in `deploy/.env` on the box and are not in the repository. `data/` and
`*.db` are gitignored and hold real Riot IDs; keep them out of commits and out of
anything that leaves the machine.

## The traps

**ffmpeg 5.1 cannot input-seek into a lone fMP4 fragment.** Debian bookworm ships 5.1,
and `vod.py` is written for it. In `_frame_from_segment` the `-ss` goes *after* `-i`, not
before. Before `-i` it returns zero bytes with `Invalid NAL unit size`, and there is a
test asserting the argument order. The first version of that fix was verified with
offsets that all happened to land on segment boundaries, where the remainder is 0.0 and
the bug cannot appear — if you are checking seek behaviour, use offsets that are not
multiples of the segment length.

**Never downscale frames before OCR.** It looks like free speed and the box counts
barely move, but strings corrupt silently: `imjustawawffle` came back as
`imjustawawfle`. A mangled name fails `names_present` and reads as a streamer who was
not there.

**HenrikDev's stored-matches is an accumulating subset**, not a mirror of Riot's
history, and it pages at 100. Never assume one call is the whole window; `_list_window`
exists for this and `IngestReport.complete` reports whether it got to the bottom.

**Scans default to `mode="competitive"`**, which on a real account hides about a third
of a fortnight. If something is missing, check the queue before suspecting the matcher.

**Rate limits.** HenrikDev Basic is 30 points a minute; Twitch's app token is a bucket
of roughly 800 points a minute. Both clients watch the response headers and sleep rather
than fail, so "HenrikDev allows 30 calls a minute and this scan has used them" in the
log is the client working, not an error. Deeper windows make it more likely because
candidate lookup runs eight threads wide.

The HenrikDev window belongs to the *key*, not to the client object, and a scan builds
three clients out of one key. `henrik.budget_for` is the shared state they all pace
against; it holds an absolute deadline rather than the header's countdown, because a
reset read before a minute of OCR has a minute less left to wait. Measured costs on a
Basic key (2026-11-21): account 1 point, stored-matches 1-2, a match detail ~1.6 cold
and 1 warm, and `/v4/matches` **1 point for ten whole matches**. That last one is why
`_bulk_head` exists. It reaches only the ten most recent and ignores `page`, so it
supplements the listing walk rather than replacing it.

**False negatives are silent, and the three measurements have now been taken.** A
streamer wrongly rejected looks exactly like a match where nobody was streaming, so this
was measured rather than argued: 0.41% of 9,724 stored rejections were judgements at
all, 884 frames across six broadcasts found no missed streamer, and the base rate says
three per cent of the people you play with have a surviving broadcast of the match. Read
*Reading the rejections by hand* in the README before touching a verdict rule. A
demotion to "likely" still shows on the page; a rejection shows nothing.

**`looks_like_valorant` misses real Valorant screens.** `VALORANT_UI_WORDS` is
gameplay-HUD vocabulary, and a scoreboard is not a gameplay HUD: `twitch.tv/henkyyyval`
is confirmed on four lobby names read off frames with no UI word on them at all. Never
gate name evidence on it — measured, that would strip evidence from 8 of 31 confirmed
or likely encounters and destroy 2 confirmations. `other_valorant` gates on it, which is
safe only because it needs three Valorant frames and so fails silent rather than wrong.

**A lobby name is matched as a substring of the frame's text.** `shadow 斬` normalises
to `shadow` and matched inside the SMITE skin name *Izanami Shadowstalker*; 40% of the
searchable names in these databases normalise to six characters or fewer. `CERTAIN_NAMES
= 4` is the only thing standing between that and a false confirmation, so do not lower
it. The post-game anchors were measured against this before shipping: 272 anchor frames
over 73 rejections returned no lobby name on 71 of them and two on the other two.

**The gap between `started_at` and the in-match clock is 76-210 seconds, not ~109.**
`calibrate.py` was written against three broadcasts that all fell between 101 and 125,
and seventeen measured since span 75.8s to 210.2s. So `FALLBACK_SHIFT_S` is routinely
half a minute out, and **nothing may be aimed blind at a screen that is up for only a
few seconds**. That is what killed the load-in anchor: the screen listing all ten
players with their agents is real and complete - `twitch.tv/vaerie` gives nine of nine
names off one frame - and aimed with the median it settled 2 encounters in 31. Agent
select also hides the enemy team, so any pre-match frame has a ceiling of four names.
The post-game scoreboard has neither problem, which is why it is the one that shipped.

**A frame budget may be reordered but not enlarged.** Nearly every candidate is the
wrong person and spends the whole budget, so adding a frame per candidate taxes the
common case directly while the gains land only on the rare one. `END_ANCHOR_AT` frames
displace killfeed frames inside `MAX_FRAMES`; measured, rejections went from 765 frames
to 767 over 89 encounters while confirmations went from 14 to 17 over 31 and from 208
frames to 198.

**Only `/helix/users` carries a channel bio**, so `description IS NULL` in
`twitch_channel` means "never looked", not "no bio" -- every other way a channel
gets written down (a clip pool, a search hit) leaves it NULL, and
`_remember_channel` coalesces rather than overwrites for exactly this reason.
`description_fetched_at` is when the bio was read; `last_seen` is not a
substitute, because a channel is seen again every time it turns up in a clip
pool. `_fresh_bios` trusts a stored bio for `BIO_TTL_S` (a fortnight, Twitch's
own retention for an unsubscribed channel) and treats an unstamped one as
`first_seen`, the earliest it can have been read.

**The bio half of Layer 4 has never produced an encounter.** Zero of the 16,923
on disk across nineteen databases came from `clip_window+bio`; the 24
clip-derived ones all came from the name-prefix fallback. Do not delete it --
it is the only path that reaches a channel whose name says nothing about the
Riot ID, which is what Layer 4 is *for* -- but weigh any cost it adds against a
measured yield of nothing. It used to fetch about 170 bios a match for this.

**Hardware dominates everything.** One match costs ~4.7s on a 12-core laptop and ~34s on
the 1/8 OCPU box. Before concluding that something got slower, check which machine the
two numbers came from. Two separate wrong diagnoses in this repository's history were
really just this.

## Conventions

Match the house style; it is deliberate and consistent.

**Comments explain why, never what.** The code says what it does. A comment earns its
place by recording the reason, the measurement or the thing that bit someone. If a
comment would restate the line below it, delete it.

**Docstrings carry the argument.** Module and function docstrings in this codebase are
prose that explains the problem being solved, not parameter lists.

**Tests are named as contracts and read as sentences.**
`test_the_seek_is_an_output_option`, `test_a_page_parameter_that_does_nothing_stops_the_walk`.
The docstring says why the contract matters, usually by naming the failure it prevents.
Every bug fixed gets a test that would have caught it.

**Commit messages are a sentence, then the reasoning.** Imperative subject, no trailing
full stop, and a body that explains what was wrong and why this is the fix — including
the measurement, if one decided it. "Make a thirty-day window mean thirty days", not
"fix pagination". End with:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

**Measure before claiming.** This project has a documented history of confident wrong
diagnoses, every one of which was settled by a measurement that took ten minutes. If a
number appears in a comment, a doc or a commit message, it should be one that was
observed, and the thing it was observed on should be named.

**Say what was not done.** The warnings that matter most here are the ones about partial
results: a window that was truncated, a queue that was skipped, a match that was never
listed. A scan that quietly reports on a subset is the single failure mode that has
wasted the most time, so when output could mislead, make it say so.

## The budget

These are hard constraints on any fix, not preferences. A change that improves an
answer and breaks one of these has not improved anything.

**A scan must never get slower.** Whatever the current wall-clock for a scan is, that
is the ceiling; faster is the only acceptable direction. Measure both sides on the same
machine before claiming either (see *Hardware dominates everything*).

**Stay inside the quota.** Hitting a rate limit and waiting for it to reset is a bug,
not a cost of doing business. HenrikDev Basic is 30 requests a minute and Twitch's app
token is about 800 points a minute; work within them by making fewer calls, not by
sleeping longer.

**Never re-scan what is already known.** People re-enter the same Riot ID constantly,
and a full re-scan of matches already in the database spends quota and minutes to
arrive at the answer already on disk. The database is there for exactly this; if the
persistence it needs does not exist, build it.

**Test locally.** The deployed box has 952 MB of RAM and an eighth of an OCPU. Run
experiments on this machine, where neither memory nor disk nor CPU is the thing being
measured.

**Subagents run on Sonnet.** Any subagent spawned for work in this repository is
started with the Sonnet model.
