"""Detects, and can remove, duplicate PENDING go_records for the same
document -- the fingerprint left behind by retrying a sync before
pipeline.ingest_document_bytes was fixed to stop re-parsing unchanged
content (see that module's docstring).

find_duplicate_pending_records/duplicate_summary are read-only reporting.
run_cleanup is the deliberate, separate action that actually removes
records -- it never runs implicitly, and it only ever touches records
still in 'pending' status. An approved or rejected record is a real human
decision, not a resync artifact, and is never a candidate here regardless
of how many pending duplicates sit alongside it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .. import audit
from ..db import utcnow


@dataclass(frozen=True)
class DuplicateGroup:
    document_id: int
    file_name: str
    source_name: str
    department_bucket: str | None
    record_ids: list[int]  # oldest first
    keep_id: int  # the newest -- what a cleanup would keep


def find_duplicate_pending_records(conn: sqlite3.Connection) -> list[DuplicateGroup]:
    """One entry per document that currently has more than one 'pending'
    go_records row -- never an approved/rejected one, since those are real
    decisions, not resync artifacts, and this must never suggest touching
    them."""
    rows = conn.execute(
        """
        SELECT r.document_id, d.file_name, s.name AS source_name, dc.department_bucket,
               GROUP_CONCAT(r.id) AS record_ids
          FROM go_records r
          JOIN documents d ON d.id = r.document_id
          JOIN sources s ON s.id = r.source_id
          LEFT JOIN document_categories dc ON dc.document_id = r.document_id
         WHERE r.status = 'pending'
         GROUP BY r.document_id
        HAVING COUNT(*) > 1
         ORDER BY COUNT(*) DESC
        """
    ).fetchall()
    groups = []
    for row in rows:
        ids = sorted(int(x) for x in row["record_ids"].split(","))
        groups.append(
            DuplicateGroup(
                document_id=int(row["document_id"]),
                file_name=row["file_name"],
                source_name=row["source_name"],
                department_bucket=row["department_bucket"],
                record_ids=ids,
                keep_id=ids[-1],
            )
        )
    return groups


def duplicate_summary(conn: sqlite3.Connection) -> dict:
    groups = find_duplicate_pending_records(conn)
    total_removable = sum(len(g.record_ids) - 1 for g in groups)
    by_department: dict[str, int] = {}
    for g in groups:
        key = g.department_bucket or "uncategorized"
        by_department[key] = by_department.get(key, 0) + (len(g.record_ids) - 1)
    return {
        "documents_with_duplicates": len(groups),
        "records_removable": total_removable,
        "by_department": by_department,
        "groups": groups,
    }


def delete_record_and_its_extraction(conn: sqlite3.Connection, record_id: int) -> None:
    """Removes one go_records row and every table that hangs off it or its
    extraction, in FK-safe order. agent_sync_log is history, not something
    this touches by deleting -- its go_record_id just gets nulled out so it
    doesn't dangle, the same way an audit trail can reference something
    since removed without that being an error."""
    row = conn.execute("SELECT extraction_id FROM go_records WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        return
    extraction_id = int(row["extraction_id"])

    conn.execute("UPDATE agent_sync_log SET go_record_id = NULL WHERE go_record_id = ?", (record_id,))
    conn.execute("DELETE FROM saved_records WHERE record_id = ?", (record_id,))
    conn.execute("DELETE FROM download_log WHERE record_id = ?", (record_id,))
    conn.execute("DELETE FROM escalations WHERE record_id = ?", (record_id,))
    conn.execute("DELETE FROM go_field_candidates WHERE record_id = ?", (record_id,))
    conn.execute("DELETE FROM go_fields WHERE record_id = ?", (record_id,))
    conn.execute("DELETE FROM go_records WHERE id = ?", (record_id,))

    # Only remove the extraction (and its OCR/page data) if nothing else
    # still points at it -- true for every real resync duplicate (each
    # retry created its own fresh extraction row), but checked rather than
    # assumed before deleting anything shared.
    still_used = conn.execute(
        "SELECT 1 FROM go_records WHERE extraction_id = ? LIMIT 1", (extraction_id,)
    ).fetchone()
    if still_used is None:
        conn.execute(
            "DELETE FROM ocr_pages WHERE ocr_run_id IN (SELECT id FROM ocr_runs WHERE extraction_id = ?)",
            (extraction_id,),
        )
        conn.execute("DELETE FROM ocr_runs WHERE extraction_id = ?", (extraction_id,))
        conn.execute("DELETE FROM extraction_pages WHERE extraction_id = ?", (extraction_id,))
        conn.execute("DELETE FROM extractions WHERE id = ?", (extraction_id,))


def run_cleanup(conn: sqlite3.Connection, *, actor: str) -> dict:
    """Removes every duplicate PENDING go_records row found right now,
    keeping the newest per document. Safe to run more than once -- a
    document with no duplicates left is simply not touched. Every removal
    is written to the audit trail, one entry per document, naming exactly
    which record ids were removed and which was kept."""
    groups = find_duplicate_pending_records(conn)
    documents_cleaned = 0
    records_removed = 0
    for g in groups:
        to_remove = [rid for rid in g.record_ids if rid != g.keep_id]
        for record_id in to_remove:
            delete_record_and_its_extraction(conn, record_id)
        audit.record(
            conn, action="records.deduplicated", entity_type="document", entity_id=g.document_id,
            actor=actor, detail={"kept": g.keep_id, "removed": to_remove, "file_name": g.file_name},
        )
        documents_cleaned += 1
        records_removed += len(to_remove)
    return {"documents_cleaned": documents_cleaned, "records_removed": records_removed, "cleaned_at": utcnow()}
