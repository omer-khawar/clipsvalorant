# Working in this repository

Notes for Claude. Everything here is either a fact about the environment that cannot be
read off the code, or a trap that has already cost a day once. The architecture itself
is in [PLAN.md](PLAN.md), the reasoning and the measurements are in
[README.md](README.md), and the deployment is in [deploy/ORACLE.md](deploy/ORACLE.md).

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

**Rate limits.** HenrikDev Basic is 30 requests a minute; Twitch's app token is a bucket
of roughly 800 points a minute. Both clients watch the response headers and sleep rather
than fail, so "quota nearly spent, pausing 60s" in the log is the client working, not an
error. Deeper windows make it more likely because candidate lookup runs eight threads
wide.

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
