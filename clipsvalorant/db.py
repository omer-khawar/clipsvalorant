"""SQLite storage. One file, one machine - see PLAN.md section 2."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1
_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS will not
# add them to an existing database, so they are applied separately.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("highlight", "ceremony", "TEXT"),
    ("highlight", "distinct_victims", "INTEGER"),
    ("encounter", "verdict", "TEXT"),
    ("encounter", "evidence", "TEXT"),
)


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply schema.sql, then any later column additions. Idempotent."""
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    _apply_column_additions(conn)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()


def _apply_column_additions(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


@contextmanager
def session(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        init_schema(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def known_match_ids(conn: sqlite3.Connection) -> set[str]:
    return {r["id"] for r in conn.execute("SELECT id FROM match")}


def upsert_account(
    conn: sqlite3.Connection,
    *,
    puuid: str,
    name: str,
    tag: str,
    region: str | None,
    is_me: bool,
) -> None:
    now = utcnow()
    conn.execute(
        """
        INSERT INTO riot_account (puuid, name, tag, region, is_me, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(puuid) DO UPDATE SET
            name      = excluded.name,
            tag       = excluded.tag,
            region    = COALESCE(excluded.region, riot_account.region),
            is_me     = MAX(riot_account.is_me, excluded.is_me),
            last_seen = excluded.last_seen
        """,
        (puuid, name, tag, region, int(is_me), now, now),
    )
