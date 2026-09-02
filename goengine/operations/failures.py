"""Phase 3.8 Initiative 2 -- Extraction Failure Workbench.

Reads real, already-recorded pipeline failures straight from the audit log
-- pipeline.py, discovery/crawler.py, and acquisition.py already call
audit.record() at the exact moment each stage fails ("crawl.failed",
"document.download_failed", "extraction.ocr_failed", "parse.failed"). No new
failure table: a parallel one would only risk drifting from what the audit
log already says happened. Rejected go_records (a real, already-recorded
review outcome) are surfaced too, as the Publication stage.
"""

from __future__ import annotations

import json
import sqlite3
from urllib.parse import quote

STAGE_DISCOVERY = "Discovery"
STAGE_DOWNLOAD = "Download"
STAGE_OCR = "OCR"
STAGE_PARSING = "Parsing"
STAGE_PUBLICATION = "Publication"

ALL_STAGES = (STAGE_DISCOVERY, STAGE_DOWNLOAD, STAGE_OCR, STAGE_PARSING, STAGE_PUBLICATION)


def _error_message(detail_json: str | None) -> str:
    if not detail_json:
        return "(no detail recorded)"
    detail = json.loads(detail_json)
    if "error" in detail:
        return str(detail["error"])
    if "status" in detail:
        return f"HTTP {detail['status']}"
    return json.dumps(detail)


def _row(
    *, department: str, source_name: str, stage: str, error_message: str, timestamp: str | None, sort_id: int
) -> dict:
    return {
        "department": department,
        "source_name": source_name,
        "stage": stage,
        "error_message": error_message,
        "timestamp": timestamp,
        # The only real retry mechanism that exists today: launch a fresh
        # extraction request scoped to this department. No per-document
        # retry is being introduced.
        "retry_url": f"/ops/jobs?department={quote(department)}",
        # ts has only second-level precision (db.utcnow()); this internal
        # sort key breaks ties within the same second so "newest first" is
        # still correct at extraction speed. Each _*_failures() query below
        # uses its own row's primary key, which is monotonically increasing
        # within that one source table -- exact enough to order same-second
        # failures without needing a shared global sequence.
        "_sort_id": sort_id,
    }


def _discovery_failures(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.id, a.ts, a.detail, s.department AS department, s.name AS source_name
          FROM audit_log a
          JOIN sources s ON s.id = a.entity_id
         WHERE a.action = 'crawl.failed' AND a.entity_type = 'source'
        """
    ).fetchall()
    return [
        _row(department=r["department"], source_name=r["source_name"], stage=STAGE_DISCOVERY,
             error_message=_error_message(r["detail"]), timestamp=r["ts"], sort_id=int(r["id"]))
        for r in rows
    ]


def _download_failures(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.id, a.ts, a.detail, s.department AS department, s.name AS source_name
          FROM audit_log a
          JOIN discovered_documents dd ON dd.id = a.entity_id
          JOIN sources s ON s.id = dd.source_id
         WHERE a.action = 'document.download_failed' AND a.entity_type = 'discovered_document'
        """
    ).fetchall()
    return [
        _row(department=r["department"], source_name=r["source_name"], stage=STAGE_DOWNLOAD,
             error_message=_error_message(r["detail"]), timestamp=r["ts"], sort_id=int(r["id"]))
        for r in rows
    ]


def _ocr_failures(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.id, a.ts, a.detail, s.department AS department, s.name AS source_name
          FROM audit_log a
          JOIN extractions e ON e.id = a.entity_id
          JOIN documents d ON d.id = e.document_id
          JOIN sources s ON s.id = d.source_id
         WHERE a.action = 'extraction.ocr_failed' AND a.entity_type = 'extraction'
        """
    ).fetchall()
    return [
        _row(department=r["department"], source_name=r["source_name"], stage=STAGE_OCR,
             error_message=_error_message(r["detail"]), timestamp=r["ts"], sort_id=int(r["id"]))
        for r in rows
    ]


def _parse_failures(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.id, a.ts, a.detail, s.department AS department, s.name AS source_name
          FROM audit_log a
          JOIN documents d ON d.id = a.entity_id
          JOIN sources s ON s.id = d.source_id
         WHERE a.action = 'parse.failed' AND a.entity_type = 'document'
        """
    ).fetchall()
    return [
        _row(department=r["department"], source_name=r["source_name"], stage=STAGE_PARSING,
             error_message=_error_message(r["detail"]), timestamp=r["ts"], sort_id=int(r["id"]))
        for r in rows
    ]


def _publication_failures(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT r.id, r.reviewed_at, r.review_note, s.department AS department, s.name AS source_name
          FROM go_records r JOIN sources s ON s.id = r.source_id
         WHERE r.status = 'rejected'
        """
    ).fetchall()
    return [
        _row(department=r["department"], source_name=r["source_name"], stage=STAGE_PUBLICATION,
             error_message=r["review_note"] or "Rejected on review", timestamp=r["reviewed_at"],
             sort_id=int(r["id"]))
        for r in rows
    ]


def pipeline_failures(
    conn: sqlite3.Connection, *, department: str | None = None, stage: str | None = None, limit: int = 200
) -> list[dict]:
    """Every real, recorded pipeline failure across all five stages, newest
    first. `department`/`stage` narrow the same way the Review Center's
    filters do -- on the real signal, not a fabricated one."""
    failures = (
        _discovery_failures(conn) + _download_failures(conn)
        + _ocr_failures(conn) + _parse_failures(conn) + _publication_failures(conn)
    )
    if department:
        failures = [f for f in failures if f["department"] == department]
    if stage:
        failures = [f for f in failures if f["stage"] == stage]
    failures.sort(key=lambda f: (f["timestamp"] or "", f["_sort_id"]), reverse=True)
    for f in failures:
        del f["_sort_id"]
    return failures[:limit]
