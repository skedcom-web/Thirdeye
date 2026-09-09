"""Module 7 extension -- Escalation.

Additive to Phase 1's review.py: a record keeps its normal
pending/approved/rejected status, and gets an escalation flagged on top for
a higher-authority reviewer's attention. Escalating never changes the
record's own status -- it is a signal, not a decision.
"""

from __future__ import annotations

import sqlite3

from .. import audit
from ..db import utcnow
from ..review import BULK_REJECT_REASONS
from . import triage


class OperationsError(ValueError):
    pass


def escalate(
    conn: sqlite3.Connection, record_id: int, *, escalated_by: str, reason: str
) -> int:
    if not escalated_by:
        raise OperationsError("an escalator identity is required")
    if not reason:
        raise OperationsError("a reason is required to escalate")

    record = conn.execute("SELECT id FROM go_records WHERE id = ?", (record_id,)).fetchone()
    if record is None:
        raise LookupError(f"no GO record with id {record_id}")

    now = utcnow()
    cur = conn.execute(
        """
        INSERT INTO escalations (record_id, escalated_by, escalated_at, reason)
        VALUES (?, ?, ?, ?)
        """,
        (record_id, escalated_by, now, reason),
    )
    escalation_id = int(cur.lastrowid)
    audit.record(
        conn, action="record.escalated", entity_type="go_record", entity_id=record_id,
        actor=escalated_by, detail={"escalation_id": escalation_id, "reason": reason},
    )
    return escalation_id


def resolve_escalation(
    conn: sqlite3.Connection, escalation_id: int, *, resolved_by: str, note: str | None = None
) -> None:
    row = conn.execute("SELECT * FROM escalations WHERE id = ?", (escalation_id,)).fetchone()
    if row is None:
        raise LookupError(f"no escalation with id {escalation_id}")
    if not resolved_by:
        raise OperationsError("a resolver identity is required")

    conn.execute(
        "UPDATE escalations SET resolved = 1, resolved_by = ?, resolved_at = ?, resolution_note = ? WHERE id = ?",
        (resolved_by, utcnow(), note, escalation_id),
    )
    audit.record(
        conn, action="record.escalation_resolved", entity_type="go_record",
        entity_id=int(row["record_id"]), actor=resolved_by,
        detail={"escalation_id": escalation_id, "note": note},
    )


def open_escalations(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT e.*, d.file_name, s.name AS source_name
          FROM escalations e
          JOIN go_records r ON r.id = e.record_id
          JOIN documents d ON d.id = r.document_id
          JOIN sources s ON s.id = r.source_id
         WHERE e.resolved = 0
         ORDER BY e.escalated_at
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def escalations_for_record(conn: sqlite3.Connection, record_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM escalations WHERE record_id = ? ORDER BY id DESC", (record_id,)
    ).fetchall()


# ---------------------------------------------------------------------------
# Module 7 -- typed review queues
# ---------------------------------------------------------------------------
QUEUE_EXTRACTION = "extraction"
QUEUE_OCR = "ocr"
QUEUE_METADATA = "metadata"
QUEUE_FAILURE = "failure"

# Phase 4.1 Refinement 4 -- Smart Queue Segmentation. Mutually exclusive
# with each other (and with QUEUE_EXTRACTION/OCR/METADATA/FAILURE above,
# which stay confidence/needs_ocr-based and unchanged); computed by
# triage.py, not duplicated here.
QUEUE_READY_FOR_APPROVAL = triage.QUEUE_READY_FOR_APPROVAL
QUEUE_NEEDS_CORRECTION = triage.QUEUE_NEEDS_CORRECTION
QUEUE_LIKELY_NON_GO = triage.QUEUE_LIKELY_NON_GO
QUEUE_OCR_ISSUES = triage.QUEUE_OCR_ISSUES
SMART_QUEUES = triage.REVIEWER_QUEUES

# Below this confidence, a record is considered "extraction review" territory
# regardless of which specific field is weak.
LOW_CONFIDENCE_THRESHOLD = 0.7


def queue_by_type(
    conn: sqlite3.Connection, queue_type: str, *,
    department: str | None = None, limit: int = 100, offset: int = 0,
) -> list[sqlite3.Row]:
    """One page of the pending-review queue, filtered/tagged by what kind of
    attention it most likely needs -- reusing Phase 1/2 data, not a new
    parallel queue table. `department` narrows to the record's real
    registered department (`sources.department`, e.g. "Health and Family
    Welfare") -- the same names extraction requests are scoped by (see
    registry.list_departments) -- not categorize.py's coarse 4-bucket content
    classification (health/education/public_works/rural_development/other),
    which exists for the separate "Real GO Acquisition Program" tracker and
    lumps most real departments into 'other'.
    Paginated (see queue_counts for the total per queue) -- a queue of
    hundreds capped at a fixed limit with no way to reach the rest is
    exactly the bug this replaces."""
    if queue_type in SMART_QUEUES:
        rows = triage.records_in_queue(conn, queue_type, department=department, limit=limit, offset=offset)
        return [
            {
                "id": r["record_id"],
                "status": "pending",
                "file_name": r["file_name"],
                "source_name": r["source_name"],
                "extraction_confidence": r["extraction_confidence"],
                "missing_field_names": r["missing_field_names"],
                "suggested_reject_reason": r["suggested_reject_reason"],
            }
            for r in rows
        ]

    dept_clause = " AND s.department = ?" if department else ""
    dept_params = [department] if department else []

    if queue_type == QUEUE_OCR:
        return conn.execute(
            f"""
            SELECT r.id, r.status, d.file_name, s.name AS source_name, e.confidence AS extraction_confidence
              FROM go_records r
              JOIN documents d ON d.id = r.document_id
              JOIN sources s ON s.id = r.source_id
              JOIN extractions e ON e.id = r.extraction_id
             WHERE r.status = 'pending' AND e.needs_ocr = 1{dept_clause}
             ORDER BY r.id
             LIMIT ? OFFSET ?
            """,
            (*dept_params, limit, offset),
        ).fetchall()

    if queue_type == QUEUE_FAILURE:
        return conn.execute(
            f"""
            SELECT DISTINCT r.id, r.status, d.file_name, s.name AS source_name
              FROM go_records r
              JOIN documents d ON d.id = r.document_id
              JOIN sources s ON s.id = r.source_id
              JOIN extraction_failures f ON f.document_id = d.id
             WHERE r.status = 'pending'{dept_clause}
             ORDER BY r.id
             LIMIT ? OFFSET ?
            """,
            (*dept_params, limit, offset),
        ).fetchall()

    if queue_type == QUEUE_METADATA:
        # Weak on a specific field, but the text layer itself was fine --
        # distinct from an OCR problem.
        return conn.execute(
            f"""
            SELECT r.id, r.status, d.file_name, s.name AS source_name,
                   MIN(gf.confidence) AS min_field_confidence
              FROM go_records r
              JOIN documents d ON d.id = r.document_id
              JOIN sources s ON s.id = r.source_id
              JOIN extractions e ON e.id = r.extraction_id
              JOIN go_fields gf ON gf.record_id = r.id AND gf.superseded_by IS NULL
             WHERE r.status = 'pending' AND e.needs_ocr = 0{dept_clause}
             GROUP BY r.id
            HAVING MIN(gf.confidence) < ?
             ORDER BY min_field_confidence
             LIMIT ? OFFSET ?
            """,
            (*dept_params, LOW_CONFIDENCE_THRESHOLD, limit, offset),
        ).fetchall()

    # QUEUE_EXTRACTION: default -- everything pending, worst text-layer first.
    return conn.execute(
        f"""
        SELECT r.id, r.status, d.file_name, s.name AS source_name, e.confidence AS extraction_confidence
          FROM go_records r
          JOIN documents d ON d.id = r.document_id
          JOIN sources s ON s.id = r.source_id
          JOIN extractions e ON e.id = r.extraction_id
         WHERE r.status = 'pending'{dept_clause}
         ORDER BY e.confidence ASC
         LIMIT ? OFFSET ?
        """,
        (*dept_params, limit, offset),
    ).fetchall()


def queue_counts(conn: sqlite3.Connection, *, department: str | None = None) -> dict[str, int]:
    dept_clause = " AND s.department = ?" if department else ""
    dept_params = (department,) if department else ()
    counts = {
        QUEUE_EXTRACTION: conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM go_records r
              JOIN sources s ON s.id = r.source_id
             WHERE r.status = 'pending'{dept_clause}
            """,
            dept_params,
        ).fetchone()["n"],
        QUEUE_OCR: len(queue_by_type(conn, QUEUE_OCR, department=department, limit=10_000)),
        QUEUE_METADATA: len(queue_by_type(conn, QUEUE_METADATA, department=department, limit=10_000)),
        QUEUE_FAILURE: len(queue_by_type(conn, QUEUE_FAILURE, department=department, limit=10_000)),
    }
    counts.update(triage.review_segment_counts(conn, department=department))
    return counts
