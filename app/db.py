"""SQLite storage for idempotent resolution results."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import DB_PATH

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS resolutions (
    fingerprint     TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    prefer          TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    result_json     TEXT NOT NULL,
    hit_count       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_resolutions_last_seen
    ON resolutions(last_seen DESC);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def init_db(db_path: Path | str = DB_PATH) -> None:
    """Create the data directory and tables if they do not exist."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()


@contextmanager
def connect(db_path: Path | str = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def insert_if_absent(
    conn: sqlite3.Connection,
    fingerprint: str,
    status: str,
    prefer: str,
    request: dict,
    result: dict,
) -> bool:
    """Insert a fresh resolution. Returns False if the fingerprint already exists."""
    now = utc_now()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO resolutions
            (fingerprint, status, prefer, request_json, result_json,
             hit_count, created_at, last_seen)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            fingerprint,
            status,
            prefer,
            json.dumps(request, ensure_ascii=False),
            json.dumps(result, ensure_ascii=False),
            now,
            now,
        ),
    )
    conn.commit()
    return cur.rowcount == 1


def touch(conn: sqlite3.Connection, fingerprint: str) -> None:
    conn.execute(
        "UPDATE resolutions SET hit_count = hit_count + 1, last_seen = ? WHERE fingerprint = ?",
        (utc_now(), fingerprint),
    )
    conn.commit()


def get(conn: sqlite3.Connection, fingerprint: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM resolutions WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()


def list_summaries(conn: sqlite3.Connection, limit: int, offset: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT fingerprint, status, prefer, hit_count, created_at, last_seen
        FROM resolutions
        ORDER BY last_seen DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]


def row_to_response(row: sqlite3.Row, cached: bool) -> dict:
    result = json.loads(row["result_json"])
    result["fingerprint"] = row["fingerprint"]
    result["prefer"] = row["prefer"]
    result["cached"] = cached
    result["hit_count"] = row["hit_count"]
    result["created_at"] = row["created_at"]
    result["last_seen"] = row["last_seen"]
    return result
