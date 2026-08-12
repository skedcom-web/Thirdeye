"""SQLite connection handling and schema bootstrap."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import PACKAGE_DIR, Settings

SCHEMA_PATH = PACKAGE_DIR / "schema.sql"


def utcnow() -> str:
    """Timestamps are ISO-8601 UTC everywhere; audit trails need one clock."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(settings: Settings) -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


@contextmanager
def session(settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = init_db(settings)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
