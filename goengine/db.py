"""SQLite connection handling and schema bootstrap."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import PACKAGE_DIR, Settings

SCHEMA_PATH = PACKAGE_DIR / "schema.sql"
SCHEMA_PHASE2_PATH = PACKAGE_DIR / "schema_phase2.sql"
SCHEMA_PHASE3_PATH = PACKAGE_DIR / "schema_phase3.sql"
SCHEMA_DIAGNOSTICS_PATH = PACKAGE_DIR / "schema_diagnostics.sql"
SCHEMA_NETWORK_TESTS_PATH = PACKAGE_DIR / "schema_network_tests.sql"
SCHEMA_AGENT_PATH = PACKAGE_DIR / "schema_agent.sql"
SCHEMA_CITIZEN_PATH = PACKAGE_DIR / "schema_citizen.sql"
SCHEMA_DOCUMENT_BLOBS_PATH = PACKAGE_DIR / "schema_document_blobs.sql"

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

# Modules 1-5 (Phase 3): geography linkage and the operational lifecycle
# state, distinct from the certification_status *result* columns Phase 2
# already added.
SOURCES_PHASE3_COLUMNS: dict[str, str] = {
    "state_id": "INTEGER REFERENCES states(id)",
    "district_id": "INTEGER REFERENCES districts(id)",
    "discovery_method": "TEXT",
    "lifecycle_status": "TEXT NOT NULL DEFAULT 'NEW'",
    "current_version": "INTEGER NOT NULL DEFAULT 1",
}

# Phase 3.3: the Source Registry blueprint's Priority and Source Type fields.
# `source_type` already exists (go_portal | gazette | department_site) with a
# CHECK constraint baked into the original CREATE TABLE and drives no other
# logic beyond that validation, so widening it to the blueprint's full
# taxonomy (GO Portal, Department Portal, District Portal, Gazette, Tender
# Portal, Scheme Portal, Notification Portal) as a *new* column -- validated
# in Python, same as `discovery_method` already is -- avoids an ALTER-TABLE
# rebuild of a table every other Phase 1-3 migration has only ever added
# columns to.
SOURCES_PHASE33_COLUMNS: dict[str, str] = {
    "priority": "TEXT NOT NULL DEFAULT 'Medium'",
    "source_category": "TEXT",
}

CRAWL_RUNS_DIAGNOSTICS_COLUMNS: dict[str, str] = {
    "pages_visited": "INTEGER NOT NULL DEFAULT 0",
    "dept_pages_found": "INTEGER NOT NULL DEFAULT 0",
    "go_listings_found": "INTEGER NOT NULL DEFAULT 0",
    "doc_pages_found": "INTEGER NOT NULL DEFAULT 0",
    "doc_links_found": "INTEGER NOT NULL DEFAULT 0",
    "pdf_links_found": "INTEGER NOT NULL DEFAULT 0",
    "downloaded_count": "INTEGER NOT NULL DEFAULT 0",
    "parsed_count": "INTEGER NOT NULL DEFAULT 0",
    "ocr_count": "INTEGER NOT NULL DEFAULT 0",
    "parser_failures": "INTEGER NOT NULL DEFAULT 0",
    "download_failures": "INTEGER NOT NULL DEFAULT 0",
    "rejected_links": "INTEGER NOT NULL DEFAULT 0",
    "skipped_links": "INTEGER NOT NULL DEFAULT 0",
    "proxy_used": "TEXT",
    "ssl_fallback_used": "INTEGER NOT NULL DEFAULT 0",
    "user_agent": "TEXT",
}

SOURCES_DIAGNOSTICS_COLUMNS: dict[str, str] = {
    "ssl_verification_enabled": "INTEGER NOT NULL DEFAULT 1",
    "allow_ssl_fallback": "INTEGER NOT NULL DEFAULT 0",
}

# Root Cause Classification Framework fields (operations/network_diagnosis.py).
# `failure_category` already existed and used to hold the literal string
# "network_failure" for almost everything; it now holds one of the fourteen
# definitive root-cause categories instead. These three columns are what
# make "last successful stage" / "failure stage" / "confidence" queryable
# rather than only visible if you read `error_message` by eye.
ROOT_CAUSE_COLUMNS: dict[str, str] = {
    "last_successful_stage": "TEXT",
    "failure_stage": "TEXT",
    "confidence_level": "TEXT",
    "exception_type": "TEXT",
    "exception_message": "TEXT",
}

# Comparative diagnostics (requirement 5): the conclusion drawn from
# comparing a target against control targets in the same run. Only
# meaningful for network_connectivity_tests, which is the only table that
# runs control targets alongside real targets in one batch.
NETWORK_TEST_COMPARISON_COLUMNS: dict[str, str] = {
    "comparison_conclusion": "TEXT",
}

# Phase 3.4 -- Local Extraction Agent. `source` distinguishes an OCR run
# produced live on this machine (the normal Render path -- when Tesseract is
# actually installed) from one submitted pre-computed by a local agent
# syncing a document it already OCR'd on hardware that has Tesseract.
OCR_RUNS_AGENT_COLUMNS: dict[str, str] = {
    "source": "TEXT NOT NULL DEFAULT 'server'",
}

# Local-only bookkeeping for `thirdeye sync`: which locally-downloaded
# documents have already been pushed to the cloud portal. Meaningless (just
# an unused column) on the cloud DB itself, since init_db() is identical on
# both sides.
DOCUMENTS_AGENT_SYNC_COLUMNS: dict[str, str] = {
    "agent_synced_at": "TEXT",
    "agent_sync_error": "TEXT",
}

# Distinguishes a normal scoped extraction request from a "resync_all"
# request: the latter carries no state/district/department scope and tells
# the local agent to reset its own local sync bookkeeping and re-push every
# archived document, rather than crawl/download anything. See
# operations/reset.py -- a production reset lives only on the server, so it
# can't clear the *local* agent's agent_synced_at column; queuing this kind
# of request is how the server asks the local machine to do that itself.
EXTRACTION_REQUESTS_KIND_COLUMNS: dict[str, str] = {
    "kind": "TEXT NOT NULL DEFAULT 'extraction'",
}


def utcnow() -> str:
    """Timestamps are ISO-8601 UTC everywhere; audit trails need one clock."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    """Local SQLite by default (dev/tests -- unchanged). If
    TURSO_DATABASE_URL/TURSO_AUTH_TOKEN are set, connects to a remote Turso
    database instead: the free-tier Render disk this local file would
    otherwise live on doesn't survive a redeploy, so production points here
    instead. See goengine/turso_db.py for why it's a remote-only connection
    (no local replica, no sync() durability gap) and exactly what subset of
    the sqlite3 API it mirrors."""
    turso_url = os.environ.get("TURSO_DATABASE_URL")
    turso_token = os.environ.get("TURSO_AUTH_TOKEN")
    if turso_url and turso_token:
        from .turso_db import TursoConnection

        return TursoConnection(turso_url, turso_token)  # type: ignore[return-value]

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


def _seed_tamil_nadu(conn: sqlite3.Connection) -> None:
    now_ts = utcnow()
    # Insert State
    conn.execute(
        "INSERT OR IGNORE INTO states (name, code, status, active, created_at) VALUES (?, ?, ?, ?, ?)",
        ("Tamil Nadu", "TN", "ACTIVE", 1, now_ts)
    )
    state_row = conn.execute("SELECT id FROM states WHERE code = 'TN'").fetchone()
    if not state_row:
        return
    state_id = state_row["id"]

    # Insert 38 Districts of Tamil Nadu
    districts = [
        ("Ariyalur", "AR"), ("Chengalpattu", "CGL"), ("Chennai", "CHN"), ("Coimbatore", "CBE"),
        ("Cuddalore", "CUD"), ("Dharmapuri", "DPI"), ("Dindigul", "DGL"), ("Erode", "ERD"),
        ("Kallakurichi", "KKI"), ("Kanchipuram", "KPM"), ("Kanyakumari", "KK"), ("Karur", "KRR"),
        ("Krishnagiri", "KGI"), ("Madurai", "MDU"), ("Mayiladuthurai", "MYD"), ("Nagapattinam", "NGP"),
        ("Namakkal", "NKL"), ("Nilgiris", "NIL"), ("Perambalur", "PBL"), ("Pudukkottai", "PDK"),
        ("Ramanathapuram", "RMD"), ("Ranipet", "RPT"), ("Salem", "SLM"), ("Sivaganga", "SVG"),
        ("Tenkasi", "TKS"), ("Thanjavur", "TJV"), ("Theni", "TNI"), ("Thoothukudi", "TUT"),
        ("Tiruchirappalli", "TRY"), ("Tirunelveli", "TNV"), ("Tirupathur", "TPT"), ("Tiruppur", "TPU"),
        ("Tiruvallur", "TLR"), ("Tiruvannamalai", "TVM"), ("Tiruvarur", "TVR"), ("Vellore", "VEL"),
        ("Viluppuram", "VPM"), ("Virudhunagar", "VDG")
    ]
    for name, code in districts:
        conn.execute(
            "INSERT OR IGNORE INTO districts (state_id, name, code, status, certification_status, publication_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (state_id, name, code, "CONFIGURED", "PENDING", "NOT_PUBLISHED", now_ts)
        )

    # Insert Official Sources from registry
    from . import registry
    year = registry._year_param()
    for spec in registry.SEED_SOURCES:
        name = spec["name"]
        department = spec["department"]
        if "dep_id" in spec:
            url = f"https://www.tn.gov.in/go.php?dep_id={spec['dep_id']}&year={year}"
            src_type = "department_site"
            src_category = "Department Portal"
            priority = "High"
        else:
            url = spec["url"].format(year=year)
            src_type = spec["source_type"]
            src_category = spec["source_category"]
            priority = spec["priority"]
        
        adapter = "tn_go_portal"
        
        existing = conn.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
        if existing is None:
            host = url.split("//")[-1].split("/")[0]
            cur = conn.execute(
                """
                INSERT INTO sources
                    (name, department, url, host, source_type, adapter, active,
                     crawl_frequency, priority, source_category, state_id, discovery_method, lifecycle_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, 'daily', ?, ?, ?, 'listing_page', 'ACTIVE', ?)
                """,
                (name, department, url, host, src_type, adapter, priority, src_category, state_id, now_ts)
            )
            source_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO source_versions
                    (source_id, version, name, department, url, discovery_method, active,
                     crawl_frequency, changed_by, changed_at, change_reason)
                VALUES (?, 1, ?, ?, ?, 'listing_page', 1, 'daily', 'system', ?, 'initial seed')
                """,
                (source_id, name, department, url, now_ts)
            )


def init_db(settings: Settings) -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.executescript(SCHEMA_PHASE2_PATH.read_text(encoding="utf-8"))
    _ensure_columns(conn, "sources", SOURCES_PHASE2_COLUMNS)
    _ensure_columns(conn, "extractions", EXTRACTIONS_PHASE2_COLUMNS)
    _ensure_columns(conn, "extraction_pages", EXTRACTION_PAGES_PHASE2_COLUMNS)
    conn.executescript(SCHEMA_PHASE3_PATH.read_text(encoding="utf-8"))
    _ensure_columns(conn, "sources", SOURCES_PHASE3_COLUMNS)
    _ensure_columns(conn, "sources", SOURCES_PHASE33_COLUMNS)
    conn.executescript(SCHEMA_DIAGNOSTICS_PATH.read_text(encoding="utf-8"))
    _ensure_columns(conn, "crawl_runs", CRAWL_RUNS_DIAGNOSTICS_COLUMNS)
    _ensure_columns(conn, "sources", SOURCES_DIAGNOSTICS_COLUMNS)
    _ensure_columns(conn, "crawl_evidences", ROOT_CAUSE_COLUMNS)
    conn.executescript(SCHEMA_NETWORK_TESTS_PATH.read_text(encoding="utf-8"))
    _ensure_columns(conn, "network_connectivity_tests", ROOT_CAUSE_COLUMNS)
    _ensure_columns(conn, "network_connectivity_tests", NETWORK_TEST_COMPARISON_COLUMNS)
    conn.executescript(SCHEMA_AGENT_PATH.read_text(encoding="utf-8"))
    _ensure_columns(conn, "ocr_runs", OCR_RUNS_AGENT_COLUMNS)
    _ensure_columns(conn, "documents", DOCUMENTS_AGENT_SYNC_COLUMNS)
    _ensure_columns(conn, "extraction_requests", EXTRACTION_REQUESTS_KIND_COLUMNS)
    conn.executescript(SCHEMA_CITIZEN_PATH.read_text(encoding="utf-8"))
    conn.executescript(SCHEMA_DOCUMENT_BLOBS_PATH.read_text(encoding="utf-8"))

    # Automatically seed Tamil Nadu if table is empty and we are not in test mode
    if "PYTEST_CURRENT_TEST" not in os.environ:
        states_count = conn.execute("SELECT COUNT(*) AS n FROM states").fetchone()["n"]
        if states_count == 0:
            _seed_tamil_nadu(conn)

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
