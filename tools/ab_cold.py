"""The A/B I should have run: anchors on and off against a COLD frame cache.

The one that shipped used data/frames, which already held every killfeed frame
these encounters had ever read. That measured OCR on segments already on disk
and could not see the thing the anchors actually change: not how many frames
are read, but WHERE in the broadcast they are. The killfeed offsets cluster
around the kills; an anchor sits past the end of the match, in a part of the
VOD nothing has fetched. Same count, different segments to download.

Each arm here gets its own empty cache, so every frame is paid for in full,
and the arms alternate order so network drift does not land on one of them.
"""
import io, json, shutil, sqlite3, sys, tempfile, time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from clipsvalorant import calibrate
from clipsvalorant.resolve import build_watcher
from clipsvalorant.vision import wanted_names
from clipsvalorant.watch import watch

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 8
rows = []
for path in sorted(Path("data").glob("*.db")):
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        got = conn.execute(
            """SELECT e.riot_id, e.channel_login, e.vod_id, e.match_id, m.map,
                      m.started_at_epoch AS start, m.game_length_ms,
                      v.created_epoch, v.end_epoch,
                      (SELECT shift_s FROM match_vod_offset o
                        WHERE o.match_id = e.match_id AND o.vod_id = e.vod_id
                          AND o.method = 'measured') AS shift
               FROM encounter e JOIN match m ON m.id = e.match_id
               JOIN twitch_vod v ON v.id = e.vod_id
               WHERE m.game_length_ms IS NOT NULL AND e.verdict = 'rejected'""").fetchall()
    except sqlite3.Error:
        conn.close(); continue
    for row in got:
        players = [(r["name"], bool(r["is_me"])) for r in conn.execute(
            "SELECT name, is_me FROM match_player WHERE match_id = ?", (row["match_id"],))]
        kills = [(k["round"], k["time_in_match_ms"] / 1000) for k in conn.execute(
            "SELECT round, time_in_match_ms FROM kill WHERE match_id = ? "
            "ORDER BY time_in_match_ms", (row["match_id"],))]
        lobby = wanted_names(players)
        me = next((n for n, mine in players if mine), "")
        if lobby and kills:
            rows.append((dict(row), lobby, kills, me))
    conn.close()

seen_vods = set()
picked = []
for r in rows:                      # one encounter per VOD, so arms never share a fetch
    if r[0]["vod_id"] in seen_vods:
        continue
    seen_vods.add(r[0]["vod_id"])
    picked.append(r)
    if len(picked) >= LIMIT:
        break


def arm(row, lobby, kills, me, *, anchors):
    """One cold run. Returns (seconds, frames attempted, files written)."""
    cache = Path(tempfile.mkdtemp(prefix="coldframes"))
    try:
        watcher, why = build_watcher(cache)
        if watcher is None:
            sys.exit(f"no watcher: {why}")
        start, vod_end = row["start"], row["end_epoch"]
        end = start + (row["game_length_ms"] or 0) // 1000
        shift = row["shift"] if row["shift"] is not None else calibrate.FALLBACK_SHIFT_S
        base = start - row["created_epoch"] + shift
        lo = max(0.0, row["created_epoch"] - start - shift)
        hi = min(float(end - start), vod_end - start - shift)
        length = float(end - start)
        t0 = time.perf_counter()
        seen = watch(
            watcher.reader, watcher.frames, row["vod_id"], base, kills, lobby, me,
            window=(lo, hi), exclude_name=row["riot_id"].partition("#")[0],
            match_end_s=length if anchors else 0.0,
            tail_s=max(0.0, (vod_end - start - shift) - length) if anchors else 0.0,
        )
        secs = time.perf_counter() - t0
        files = len(list(cache.glob("*")))
        return secs, seen.frames_read + seen.frames_failed, files, len(seen.names)
    finally:
        shutil.rmtree(cache, ignore_errors=True)


print(f"{len(picked)} rejected encounter(s), each run twice from an empty cache\n",
      flush=True)
tot = {"off": [0.0, 0, 0], "on": [0.0, 0, 0]}
for i, (row, lobby, kills, me) in enumerate(picked):
    order = ["off", "on"] if i % 2 == 0 else ["on", "off"]
    res = {}
    for which in order:
        res[which] = arm(row, lobby, kills, me, anchors=(which == "on"))
    for which in ("off", "on"):
        s, f, fl, _ = res[which]
        tot[which][0] += s; tot[which][1] += f; tot[which][2] += fl
    print(f"  {row['channel_login'][:18]:18s} {row['map'][:9]:9s} "
          f"off {res['off'][0]:6.1f}s/{res['off'][1]:2d}f   "
          f"on {res['on'][0]:6.1f}s/{res['on'][1]:2d}f   "
          f"({'+' if res['on'][0] > res['off'][0] else ''}"
          f"{res['on'][0] - res['off'][0]:.1f}s)", flush=True)

print()
for which in ("off", "on"):
    s, f, fl = tot[which]
    print(f"  anchors {which:3s}  {s:7.1f}s total, {f:3d} frames attempted, "
          f"{fl:3d} cache files written, {s / max(f, 1):.2f}s a frame")
d = tot["on"][0] - tot["off"][0]
print(f"\n  cold cost of the anchors: {d:+.1f}s over {len(picked)} rejections "
      f"({d / max(tot['off'][0], 1) * 100:+.0f}%)")
Path(sys.argv[2] if len(sys.argv) > 2 else "ab_cold.json").write_text(
    json.dumps(tot, indent=1), encoding="utf-8")
