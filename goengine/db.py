"""SQLite connection handling and schema bootstrap."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import PACKAGE_DIR, Settings

SCHEMA_PATH = PACKAGE_DIR / "schema.sql"
SCHEMA_PHASE2_PATH = PACKAGE_DIR / "schema_phase2.sql"

# Columns added to the pre-existing `sources` table for Phase 1 certification.
# SQLite's ALTER TABLE has no "ADD COLUMN IF NOT EXISTS", so this is applied
# idempotently in Python via PRAGMA table_info introspection instead of SQL.
SOURCES_PHASE2_COLUMNS: dict[str, str] = {
    "certification_status": "TEXT NOT NULL DEFAULT 'PENDING'",
    "certification_date": "TEXT",
    "last_crawl_success_at": "TEXT",
    "last_crawl_failure_at": "TEXT",
}

# Module 4 (OCR): which pages ended up OCR'd, and what the merged extraction
# looked like afterward.
EXTRACTIONS_PHASE2_COLUMNS: dict[str, str] = {
    "ocr_applied": "INTEGER NOT NULL DEFAULT 0",
    "ocr_pages_count": "INTEGER NOT NULL DEFAULT 0",
}
EXTRACTION_PAGES_PHASE2_COLUMNS: dict[str, str] = {
    "source": "TEXT NOT NULL DEFAULT 'digital'",
}


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


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")


def init_db(settings: Settings) -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.executescript(SCHEMA_PHASE2_PATH.read_text(encoding="utf-8"))
    _ensure_columns(conn, "sources", SOURCES_PHASE2_COLUMNS)
    _ensure_columns(conn, "extractions", EXTRACTIONS_PHASE2_COLUMNS)
    _ensure_columns(conn, "extraction_pages", EXTRACTION_PAGES_PHASE2_COLUMNS)
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
