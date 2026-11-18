"""Compare our highlight detection against tracker.gg's own per-match tags.

tracker.gg is the better oracle than Riot's round ceremony: it tags every match with
multi-kills and clutches, and its tag descriptions state the definitions exactly
("Killed 3 enemies in one round", "Won a 1v1 clutch"). Riot's ceremony is one per
round and goes to the winning team, so it silently omits an ace in a lost round.

    python tools/compare_tracker.py [--truth data/tracker/tracker_truth.txt]

Matches are joined on start time to the minute, since tracker renders local time but
stores UTC, and our own records come from HenrikDev in UTC.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipsvalorant import db  # noqa: E402
from clipsvalorant.config import load_config  # noqa: E402

# tracker tag code -> how we name the same thing
MULTI = {"3": 3, "4": 4, "5": 5, "6": 6, "7": 7}
CLUTCH_WON = {"a": 1, "b": 2, "c": 3, "d": 4}
CLUTCH_LOST = {"A": 1, "B": 2, "C": 3, "D": 4}
IGNORED = {"K"}  # highKast is not a moment we can clip


def parse_truth(path: Path) -> dict[str, dict[str, int]]:
    """-> {'YYYYMMDDHHMM': {'multi3': 2, 'clutch1': 1, 'clutchlost1': 1, 'won': 1}}"""
    out: dict[str, dict[str, int]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for token in line.split():
            stamp, outcome, rest = token[:12], token[12], token[13:]
            tags: dict[str, int] = {"won": 1 if outcome == "W" else 0}
            i = 0
            while i < len(rest) - 1:
                code, count = rest[i], int(rest[i + 1])
                if code in MULTI:
                    tags[f"multi{MULTI[code]}"] = tags.get(f"multi{MULTI[code]}", 0) + count
                elif code in CLUTCH_WON:
                    tags[f"clutch{CLUTCH_WON[code]}"] = count
                elif code in CLUTCH_LOST:
                    tags[f"clutchlost{CLUTCH_LOST[code]}"] = count
                elif code not in IGNORED:
                    tags[f"unknown_{code}"] = count
                i += 2
            out[stamp] = tags
    return out


def ours(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    rows = conn.execute(
        """
        SELECT m.started_at_epoch, m.mode, m.map, h.kind, h.label, h.kill_count
        FROM match m LEFT JOIN highlight h ON h.match_id = m.id
        WHERE m.mode = 'Competitive' OR m.queue = 'Competitive'
        """
    ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        stamp = datetime.fromtimestamp(r["started_at_epoch"], tz=timezone.utc).strftime(
            "%Y%m%d%H%M"
        )
        bucket = out.setdefault(stamp, {})
        if r["kind"] is None:
            continue
        if r["kind"] in ("triple", "quadra", "ace", "multi"):
            key = f"multi{r['kill_count']}"
        elif r["kind"] == "clutch":
            key = f"clutch{_enemies(r['label'])}"
        elif r["kind"] == "clutch_lost":
            key = f"clutchlost{_enemies(r['label'])}"
        else:
            key = r["kind"]
        bucket[key] = bucket.get(key, 0) + 1
    return out


def _enemies(label: str) -> int:
    # labels look like "1v3 clutch" / "1v3 lost"
    try:
        return int(label.split("v")[1].split()[0])
    except (IndexError, ValueError):
        return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth", default="data/tracker/tracker_truth.txt")
    ap.add_argument("--verbose", action="store_true", help="show matching rows too")
    args = ap.parse_args()

    cfg = load_config(require_api_key=False)
    truth = parse_truth(Path(args.truth))

    with db.session(cfg.db_path) as conn:
        mine = ours(conn)

    shared = sorted(set(truth) & set(mine), reverse=True)
    print(f"tracker rows: {len(truth)}   our competitive matches: {len(mine)}   "
          f"joined on timestamp: {len(shared)}\n")

    missing_from_db = sorted(set(truth) - set(mine), reverse=True)
    if missing_from_db:
        print(f"in tracker but not ingested ({len(missing_from_db)}): "
              f"{', '.join(missing_from_db[:8])}\n")

    agree = 0
    disagreements: Counter[str] = Counter()
    for stamp in shared:
        t = {k: v for k, v in truth[stamp].items() if k != "won"}
        o = dict(mine[stamp])
        # tracker has no 'lost clutch' tag beyond 1v1, so ignore ours above that
        for key in list(o):
            if key.startswith("clutchlost") and key not in t and key != "clutchlost1":
                o.pop(key)
        if t == o:
            agree += 1
            if args.verbose:
                print(f"  OK   {stamp}  {t or '(nothing)'}")
            continue
        only_t = {k: v for k, v in t.items() if o.get(k) != v}
        only_o = {k: v for k, v in o.items() if t.get(k) != v}
        for k in set(only_t) | set(only_o):
            kind = "multi" if k.startswith("multi") else (
                "clutch_lost" if k.startswith("clutchlost") else "clutch")
            disagreements[kind] += 1
        print(f"  DIFF {stamp}")
        print(f"       tracker: {only_t}")
        print(f"       ours   : {only_o}")

    print(f"\n=== {agree}/{len(shared)} matches agree exactly ===")
    if disagreements:
        print(f"disagreement types: {dict(disagreements)}")


if __name__ == "__main__":
    main()
