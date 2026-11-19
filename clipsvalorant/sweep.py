"""C7 - the unattended run: ingest new matches, then resolve the ones worth resolving.

This exists because of a measured fact rather than a design preference. Twitch keeps
past broadcasts for 7 days, 14 for Affiliates, Partners and Turbo subscribers, and
only when the streamer has VOD saving switched on. Over 44 matches inside that
nominal window, 385 of 391 candidates had no surviving broadcast at all and only 12
of 4,246 channels kept any archive. Nothing in the identity funnel can recover a
match whose VODs have already expired, so the deciding factor is simply *when* you
look. Running this on a schedule is the feature; the funnel is just the mechanism.

Which matches are due, and why:

  * Older than RETENTION_DAYS - never. There is nothing left to find, and asking
    costs a Twitch call per player.
  * Never resolved - always.
  * Young, unconfirmed and last looked at a while ago - again, up to a few times. A
    VOD is published when the broadcast ends, which can be hours after the match,
    and viewers keep cutting clips out of it for days afterwards. The evidence that
    confirms a streamer therefore accumulates *after* the match, so one look the
    moment it is ingested is not enough.
  * Already confirmed, or out of attempts - no. Stop spending quota on a settled
    question.

Matches with no highlights of your own are skipped too: finding a streamer in that
lobby would produce nothing to link to.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field

from clipsvalorant import db
from clipsvalorant.config import Config
from clipsvalorant.ingest import ingest
from clipsvalorant.resolve import ResolveReport, resolve_recent

log = logging.getLogger(__name__)

# Twitch's longest documented retention. Past this, a match is beyond help.
RETENTION_DAYS = 14
# How long a match stays worth re-checking, and how often, and how many times.
RECHECK_DAYS = 4
RECHECK_INTERVAL_H = 20
MAX_ATTEMPTS = 5


@dataclass
class SweepReport:
    listed: int = 0
    new_matches: int = 0
    new_highlights: int = 0
    due: int = 0
    dry_run: bool = False
    skipped_expired: int = 0
    skipped_settled: int = 0
    resolve: ResolveReport | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def confirmed(self) -> list:
        return list(self.resolve.confirmed) if self.resolve else []

    def summary(self) -> str:
        if self.dry_run:
            parts = [f"would resolve {self.due} match(es)"]
            if self.skipped_expired:
                parts.append(f"{self.skipped_expired} past VOD retention")
            if self.skipped_settled:
                parts.append(f"{self.skipped_settled} already settled")
            return ", ".join(parts) + "  (dry run, nothing fetched)"
        parts = [
            f"{self.listed} listed",
            f"{self.new_matches} new match(es)",
            f"{self.new_highlights} new highlight(s)",
            f"{self.due} resolved",
        ]
        if self.skipped_expired:
            parts.append(f"{self.skipped_expired} past VOD retention")
        if self.skipped_settled:
            parts.append(f"{self.skipped_settled} already settled")
        if self.resolve:
            parts.append(f"{len(self.resolve.confirmed)} confirmed")
            if self.resolve.candidates:
                parts.append(f"{len(self.resolve.candidates)} unverified")
        return ", ".join(parts)


def matches_due(conn: sqlite3.Connection, *, now: int | None = None) -> tuple[list[str], int, int]:
    """Which matches to resolve this run. Returns (ids, expired, settled)."""
    now = int(time.time()) if now is None else now
    rows = conn.execute(
        """
        SELECT m.id, m.started_at_epoch, m.last_resolved_at, m.resolve_attempts,
               (SELECT COUNT(*) FROM highlight h WHERE h.match_id = m.id) AS highlights,
               (SELECT COUNT(*) FROM encounter e
                 WHERE e.match_id = m.id AND e.verdict = 'confirmed') AS confirmed
        FROM match m
        ORDER BY m.started_at_epoch DESC
        """
    ).fetchall()

    due: list[str] = []
    expired = settled = 0
    for row in rows:
        age_days = (now - row["started_at_epoch"]) / 86400
        if age_days > RETENTION_DAYS:
            expired += 1
            continue
        if not row["highlights"]:
            # Nothing of yours happened here, so there is nothing to link to.
            settled += 1
            continue
        if row["confirmed"] or row["resolve_attempts"] >= MAX_ATTEMPTS:
            settled += 1
            continue
        if row["last_resolved_at"] is None:
            due.append(row["id"])
            continue
        if age_days > RECHECK_DAYS:
            settled += 1
            continue
        last = db.to_epoch(row["last_resolved_at"])
        if last is None or (now - last) >= RECHECK_INTERVAL_H * 3600:
            due.append(row["id"])
        else:
            settled += 1
    return due, expired, settled


def sweep(cfg: Config, *, limit: int = 20, mode: str | None = None,
          dry_run: bool = False) -> SweepReport:
    """One unattended pass: pull what is new, then look at what is worth looking at.

    `dry_run` reports which matches this run would resolve and then stops,
    without spending a single API call. Worth having before putting anything on
    a timer: a schedule whose first visible output arrives tomorrow morning is
    a poor place to discover that nothing was ever due.
    """
    report = SweepReport()

    if dry_run:
        with db.session(cfg.db_path) as conn:
            due, expired, settled = matches_due(conn)
        report.due = len(due)
        report.skipped_expired = expired
        report.skipped_settled = settled
        report.dry_run = True
        return report

    try:
        ingested = ingest(cfg, limit=limit, mode=mode)
    except Exception as exc:  # a scheduled run must still try to resolve
        report.errors.append(f"ingest failed: {exc}")
        log.warning("Ingest failed, carrying on with what is already stored: %s", exc)
    else:
        report.listed = ingested.seen
        report.new_matches = ingested.stored
        report.new_highlights = ingested.highlights
        report.errors += ingested.errors

    with db.session(cfg.db_path) as conn:
        due, expired, settled = matches_due(conn)
    report.due = len(due)
    report.skipped_expired = expired
    report.skipped_settled = settled

    if not due:
        return report

    if not cfg.has_twitch:
        report.errors.append("Twitch credentials missing, so nothing was resolved.")
        return report

    try:
        report.resolve = resolve_recent(cfg, match_ids=due)
    except Exception as exc:
        report.errors.append(f"resolve failed: {exc}")
        log.warning("Resolve failed: %s", exc)
    else:
        report.errors += report.resolve.errors
    return report
