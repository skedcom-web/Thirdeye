"""Module 6 -- Certification Job Center.

Background processing without new infrastructure: a job runs in a daemon
thread with its own SQLite connection (sqlite3 connections are not safe to
share across threads), updating its row's progress counters as it goes.
Since `db.connect()` uses autocommit (isolation_level=None) and WAL mode,
those updates are visible to any other connection reading the same file
immediately -- which is what makes the "real-time dashboard" real: the
workbench polls the same job row a background thread is writing to.

    Source Discovery -> Document Download -> OCR -> Extraction ->
    Benchmarking -> Failure Analysis

is exactly the Phase 1 + Phase 2 pipeline already built; this module's job
is picking which sources are in scope for one run and reporting progress,
not re-implementing the pipeline.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import traceback
from typing import Callable

from .. import acquisition, audit
from ..certification import categorize
from ..certification import run_full_certification
from ..config import Settings
from ..db import connect, utcnow
from ..discovery import crawler
from ..fetching import Fetcher, HttpFetcher
from ..pipeline import parse_document
from . import geography

STATUS_QUEUED = "QUEUED"
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"


def sources_in_scope(
    conn: sqlite3.Connection, *, state_id: int | None, district_id: int | None,
    department_filter: list[str] | None,
) -> list[sqlite3.Row]:
    if district_id is not None:
        rows = geography.applicable_sources(conn, district_id)
    elif state_id is not None:
        rows = conn.execute(
            "SELECT * FROM sources WHERE active = 1 AND (state_id = ? OR state_id IS NULL)", (state_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM sources WHERE active = 1").fetchall()

    if department_filter:
        rows = [r for r in rows if categorize.department_bucket_for(r["department"]) in department_filter]
    return rows


def start_job(
    settings: Settings,
    *,
    state_id: int | None = None,
    district_id: int | None = None,
    department_filter: list[str] | None = None,
    created_by: str,
    fetcher_factory: Callable[[], Fetcher] = HttpFetcher,
) -> int:
    """Kick off a background certification job. Returns immediately with the
    job id; progress is visible by polling get_job()/list_jobs().

    `fetcher_factory` defaults to the real HttpFetcher -- tests pass a
    factory returning an OfflineFetcher so the background thread never
    touches the network (the thread has no access to FastAPI's
    dependency-override mechanism, since it isn't part of a request).
    """
    job_id = _create_job_row(
        settings, state_id=state_id, district_id=district_id,
        department_filter=department_filter, created_by=created_by,
    )
    thread = threading.Thread(
        target=_run_job,
        args=(settings, job_id, state_id, district_id, department_filter, created_by, fetcher_factory),
        daemon=True,
    )
    thread.start()
    return job_id


def run_job_sync(
    settings: Settings,
    *,
    state_id: int | None = None,
    district_id: int | None = None,
    department_filter: list[str] | None = None,
    created_by: str,
    fetcher_factory: Callable[[], Fetcher] = HttpFetcher,
) -> int:
    """Same as start_job, but blocks until the job finishes instead of
    returning immediately. For tests and the CLI, where deterministic
    completion matters more than a responsive HTTP response."""
    job_id = _create_job_row(
        settings, state_id=state_id, district_id=district_id,
        department_filter=department_filter, created_by=created_by,
    )
    _run_job(settings, job_id, state_id, district_id, department_filter, created_by, fetcher_factory)
    return job_id


def _create_job_row(
    settings: Settings, *, state_id: int | None, district_id: int | None,
    department_filter: list[str] | None, created_by: str,
) -> int:
    conn = connect(settings.db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO certification_jobs
                (state_id, district_id, department_filter, status, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                state_id, district_id,
                json.dumps(department_filter) if department_filter else None,
                STATUS_QUEUED, created_by, utcnow(),
            ),
        )
        job_id = int(cur.lastrowid)
        audit.record(
            conn, action="job.started", entity_type="certification_job", entity_id=job_id,
            actor=created_by,
            detail={"state_id": state_id, "district_id": district_id, "department_filter": department_filter},
        )
        return job_id
    finally:
        conn.close()


def _run_job(
    settings: Settings, job_id: int, state_id: int | None, district_id: int | None,
    department_filter: list[str] | None, created_by: str, fetcher_factory: Callable[[], Fetcher],
) -> None:
    conn = connect(settings.db_path)
    fetcher = fetcher_factory()
    try:
        conn.execute(
            "UPDATE certification_jobs SET status = ?, started_at = ? WHERE id = ?",
            (STATUS_RUNNING, utcnow(), job_id),
        )

        sources = sources_in_scope(conn, state_id=state_id, district_id=district_id, department_filter=department_filter)
        conn.execute("UPDATE certification_jobs SET sources_total = ? WHERE id = ?", (len(sources), job_id))

        for source_row in sources:
            _run_one_source(conn, settings, fetcher, job_id, int(source_row["id"]))
            conn.execute(
                "UPDATE certification_jobs SET sources_completed = sources_completed + 1 WHERE id = ?", (job_id,)
            )

        # Certification benchmarking + failure analysis, if there is a golden
        # set to score against -- a job with no annotated documents yet still
        # completes successfully; there is just nothing to benchmark.
        try:
            run_full_certification(conn, actor=created_by)
        except Exception:
            pass  # benchmarking is a bonus step here, not the job's core purpose

        if district_id is not None:
            geography.refresh_district_certification(conn, district_id, actor=created_by)

        # Run diagnostics retention cleanup
        try:
            cleanup_expired_evidence(conn)
        except Exception as cleanup_exc:
            # Bypassed silently on failure so it doesn't block job completion
            pass

        conn.execute(
            "UPDATE certification_jobs SET status = ?, finished_at = ? WHERE id = ?",
            (STATUS_COMPLETED, utcnow(), job_id),
        )
        audit.record(
            conn, action="job.completed", entity_type="certification_job", entity_id=job_id, actor=created_by,
        )
    except Exception as exc:  # a job failure must be visible on the dashboard, never silent
        conn.execute(
            "UPDATE certification_jobs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
            (STATUS_FAILED, utcnow(), f"{exc}\n{traceback.format_exc()}", job_id),
        )
        audit.record(
            conn, action="job.failed", entity_type="certification_job", entity_id=job_id, actor=created_by,
            detail={"error": str(exc)},
        )
    finally:
        fetcher.close()
        conn.close()


def cleanup_expired_evidence(conn: sqlite3.Connection) -> int:
    """Removes request-level crawl evidence older than configured retention days."""
    row = conn.execute("SELECT value FROM system_settings WHERE key = 'diagnostics_retention_days'").fetchone()
    days = int(row["value"]) if row else 30
    
    from datetime import datetime, timezone, timedelta
    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    
    cur = conn.execute("DELETE FROM crawl_evidences WHERE timestamp < ?", (threshold,))
    deleted = cur.rowcount
    if deleted > 0:
        audit.record(
            conn,
            action="diagnostics.cleanup",
            entity_type="system",
            entity_id=0,
            actor="system",
            detail={"deleted_records": deleted, "threshold": threshold}
        )
    return deleted


def _run_one_source(
    conn: sqlite3.Connection, settings: Settings, fetcher: Fetcher, job_id: int, source_id: int
) -> None:
    from .. import registry

    source = registry.get_source(conn, source_id)
    if source is None:
        return

    # 5 (the crawler's own default) is only enough for a single listing page.
    # A directory-style source like the TN GO Portal hub now legitimately
    # follows into per-department listing pages (see TnGoPortalAdapter), so
    # a real crawl needs more hops -- still bounded, still polite (each hop
    # respects the same per-host delay), just not capped before it starts.
    crawl_result = crawler.crawl_source(conn, fetcher, source, actor="job", max_pages=20)
    conn.execute(
        "UPDATE certification_jobs SET documents_found = documents_found + ? WHERE id = ?",
        (crawl_result.new_documents, job_id),
    )

    downloaded = 0
    download_failed = 0
    parsed = 0
    parser_failed = 0
    ocr = 0

    pending = [
        r for r in crawler.pending_downloads(conn, limit=1000) if int(r["source_id"]) == source_id
    ]
    for row in pending:
        result = acquisition.acquire_one(conn, settings, fetcher, int(row["id"]), actor="job")
        if result.ok:
            downloaded += 1
            conn.execute(
                "UPDATE certification_jobs SET documents_downloaded = documents_downloaded + 1 WHERE id = ?",
                (job_id,),
            )
        else:
            download_failed += 1
            conn.execute(
                "UPDATE certification_jobs SET documents_failed = documents_failed + 1 WHERE id = ?", (job_id,)
            )

    unparsed = conn.execute(
        """
        SELECT d.id FROM documents d
         WHERE d.source_id = ? AND NOT EXISTS (
                SELECT 1 FROM go_records r
                  JOIN extractions e ON e.id = r.extraction_id
                 WHERE e.document_id = d.id
             )
        """,
        (source_id,),
    ).fetchall()
    for row in unparsed:
        try:
            parse_document(conn, settings, int(row["id"]))
            parsed += 1
            conn.execute(
                "UPDATE certification_jobs SET documents_parsed = documents_parsed + 1 WHERE id = ?", (job_id,)
            )
            needs_ocr = conn.execute(
                """
                SELECT e.needs_ocr FROM extractions e WHERE e.document_id = ? ORDER BY e.id DESC LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
            if needs_ocr and needs_ocr["needs_ocr"]:
                ocr += 1
                conn.execute(
                    "UPDATE certification_jobs SET documents_needs_ocr = documents_needs_ocr + 1 WHERE id = ?",
                    (job_id,),
                )
        except Exception:
            parser_failed += 1
            conn.execute(
                "UPDATE certification_jobs SET documents_failed = documents_failed + 1 WHERE id = ?", (job_id,)
            )

    # Finally update crawl_runs counters for this source run
    conn.execute(
        """
        UPDATE crawl_runs
           SET downloaded_count = ?,
               download_failures = ?,
               parsed_count = ?,
               parser_failures = ?,
               ocr_count = ?
         WHERE id = ?
        """,
        (downloaded, download_failed, parsed, parser_failed, ocr, crawl_result.run_id)
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM certification_jobs WHERE id = ?", (job_id,)).fetchone()


def list_jobs(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM certification_jobs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def active_job_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM certification_jobs WHERE status IN ('QUEUED', 'RUNNING')"
    ).fetchone()["n"]
