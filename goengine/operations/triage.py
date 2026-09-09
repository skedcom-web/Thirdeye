"""Phase 4.1 -- review triage: a real, tested Non-GO classifier and
Refinement 4's mutually-exclusive queue segmentation, promoted from the
Phase 4.0 assessment's one-off scratch heuristic.

Per Refinement 10, `classify_document_type` is advisory only -- nothing in
this module deletes, rejects, publishes, or approves a record. Its output is
surfaced as a suggestion (e.g. the Likely Non-GO queue's classified type,
shown so a reviewer can pick a matching Bulk Reject reason) that a human
always confirms or overrides.

Every pending record is scored against the 5 queues below in a fixed
priority order (Refinement 4), so it lands in exactly one -- the segment
functions here compute all of it with a fixed number of bulk queries
(grouped by record id / extraction id), not one query per record, following
the same pattern quality.py's department_readiness() already established.
"""

from __future__ import annotations

import re
import sqlite3

from ..config import OCR_MIN_CONFIDENCE
from ..extraction.metadata import CORE_FIELDS

QUEUE_CRITICAL = "critical_extraction_failure"
QUEUE_OCR_ISSUES = "ocr_issues"
QUEUE_LIKELY_NON_GO = "likely_non_go"
QUEUE_NEEDS_CORRECTION = "needs_correction"
QUEUE_READY_FOR_APPROVAL = "ready_for_approval"

# The 4 queues a reviewer works from in the Review Center. Critical
# Extraction Failure is deliberately excluded -- Refinement 2 routes it to
# the existing /ops/failures workbench instead.
REVIEWER_QUEUES = (QUEUE_OCR_ISSUES, QUEUE_LIKELY_NON_GO, QUEUE_NEEDS_CORRECTION, QUEUE_READY_FOR_APPROVAL)
ALL_QUEUES = (QUEUE_CRITICAL,) + REVIEWER_QUEUES

# Missing this many (or more) of the 4 core fields means extraction itself
# needs intervention -- a reviewer correcting fields one at a time isn't the
# right tool; reprocessing is (Refinement 3).
CRITICAL_MISSING_THRESHOLD = 3

GO_PATTERN = re.compile(r"G\.?O\.?\s*\(?(Ms|D|2D)?\)?\s*No\.?\s*\d+", re.IGNORECASE)
_TYPE_PATTERNS = (
    ("Circular", re.compile(r"\bcircular\b", re.IGNORECASE)),
    ("Press Release", re.compile(r"\bpress\s*release\b", re.IGNORECASE)),
    ("Tender Notice", re.compile(r"\btender\b", re.IGNORECASE)),
    ("Notification", re.compile(r"\bnotification\b", re.IGNORECASE)),
    ("Advertisement", re.compile(r"\badvertisement\b|\brecruitment\b", re.IGNORECASE)),
    ("Letter", re.compile(r"^\s*letter\b|\bletter no\b", re.IGNORECASE)),
)


def classify_document_type(subject: str | None, page1_text: str | None, go_number_present: bool) -> str:
    """Classifies a document's type from text signals alone. Advisory only
    (Refinement 10) -- callers must never use this to delete, reject,
    publish, or approve a record without a human confirming; only to
    classify, route, or prioritize."""
    combined = f"{subject or ''}\n{page1_text or ''}"
    if go_number_present or GO_PATTERN.search(combined):
        return "GO"
    for label, pattern in _TYPE_PATTERNS:
        if pattern.search(combined):
            return label
    return "Unknown"


def _segment_pending_records(conn: sqlite3.Connection, *, department: str | None = None) -> list[dict]:
    dept_clause = " AND s.department = ?" if department else ""
    dept_params = (department,) if department else ()
    records = conn.execute(
        f"""
        SELECT r.id AS record_id, r.extraction_id, s.department AS department,
               s.name AS source_name, d.file_name, e.confidence AS extraction_confidence
          FROM go_records r
          JOIN sources s ON s.id = r.source_id
          JOIN documents d ON d.id = r.document_id
          JOIN extractions e ON e.id = r.extraction_id
         WHERE r.status = 'pending'{dept_clause}
        """,
        dept_params,
    ).fetchall()
    if not records:
        return []

    record_ids = [int(r["record_id"]) for r in records]
    extraction_ids = sorted({int(r["extraction_id"]) for r in records})

    fields_by_record: dict[int, dict[str, str | None]] = {rid: {} for rid in record_ids}
    rid_placeholders = ",".join("?" * len(record_ids))
    for row in conn.execute(
        f"""
        SELECT record_id, field_name, normalized_value FROM go_fields
         WHERE record_id IN ({rid_placeholders}) AND superseded_by IS NULL
        """,
        record_ids,
    ):
        fields_by_record[int(row["record_id"])][row["field_name"]] = row["normalized_value"]

    eid_placeholders = ",".join("?" * len(extraction_ids))
    ocr_confidence_by_extraction: dict[int, float] = {}
    for row in conn.execute(
        f"""
        SELECT extraction_id, mean_word_confidence FROM ocr_runs
         WHERE extraction_id IN ({eid_placeholders})
         ORDER BY extraction_id, ran_at, id
        """,
        extraction_ids,
    ):
        # Last row per extraction wins -- the most recent OCR run.
        if row["mean_word_confidence"] is not None:
            ocr_confidence_by_extraction[int(row["extraction_id"])] = float(row["mean_word_confidence"])

    page1_by_extraction: dict[int, str] = {}
    for row in conn.execute(
        f"""
        SELECT extraction_id, text FROM extraction_pages
         WHERE extraction_id IN ({eid_placeholders}) AND page_number = 1
        """,
        extraction_ids,
    ):
        page1_by_extraction[int(row["extraction_id"])] = row["text"] or ""

    results: list[dict] = []
    for r in records:
        record_id = int(r["record_id"])
        extraction_id = int(r["extraction_id"])
        fields = fields_by_record[record_id]
        missing_field_names = [name for name in CORE_FIELDS if name not in fields]
        missing = len(missing_field_names)
        suggested_reject_reason = None

        if missing >= CRITICAL_MISSING_THRESHOLD:
            queue = QUEUE_CRITICAL
        else:
            ocr_confidence = ocr_confidence_by_extraction.get(extraction_id)
            if ocr_confidence is not None and ocr_confidence < OCR_MIN_CONFIDENCE:
                queue = QUEUE_OCR_ISSUES
            else:
                doc_type = classify_document_type(
                    fields.get("subject"), page1_by_extraction.get(extraction_id, ""), "go_number" in fields,
                )
                if doc_type != "GO":
                    queue = QUEUE_LIKELY_NON_GO
                    suggested_reject_reason = doc_type
                elif missing >= 1:
                    queue = QUEUE_NEEDS_CORRECTION
                else:
                    queue = QUEUE_READY_FOR_APPROVAL

        results.append(
            {
                "record_id": record_id,
                "department": r["department"],
                "source_name": r["source_name"],
                "file_name": r["file_name"],
                "extraction_confidence": r["extraction_confidence"],
                "queue": queue,
                "missing_core_fields": missing,
                "missing_field_names": missing_field_names,
                "suggested_reject_reason": suggested_reject_reason,
            }
        )
    return results


def review_segment_counts(conn: sqlite3.Connection, *, department: str | None = None) -> dict[str, int]:
    """Exact, non-overlapping counts for all 5 queues. Always sums to the
    total pending count for the same department scope (Refinement 4: no
    double counting, dashboard totals reconcile)."""
    counts = {q: 0 for q in ALL_QUEUES}
    for row in _segment_pending_records(conn, department=department):
        counts[row["queue"]] += 1
    return counts


def records_in_queue(
    conn: sqlite3.Connection, queue: str, *, department: str | None = None, limit: int = 100, offset: int = 0
) -> list[dict]:
    if queue not in ALL_QUEUES:
        raise ValueError(f"unknown queue {queue!r}; expected one of {ALL_QUEUES}")
    rows = [r for r in _segment_pending_records(conn, department=department) if r["queue"] == queue]
    return rows[offset : offset + limit]


def department_blocker_counts(conn: sqlite3.Connection, *, limit: int = 5) -> list[dict]:
    """Top departments by live review-failure count (everything except
    Ready For Approval) -- the same composite signal the Phase 4.0 report
    used, now computed live instead of as a one-off script."""
    counts: dict[str, int] = {}
    for r in _segment_pending_records(conn):
        if r["queue"] != QUEUE_READY_FOR_APPROVAL:
            counts[r["department"]] = counts.get(r["department"], 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return [{"department": d, "needs_attention": n} for d, n in ranked]


def top_blockers(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    """The worst records standing between the backlog and publication --
    most missing core fields first, then lowest extraction confidence.
    Ready-For-Approval records are never blockers by definition."""
    blockers = [r for r in _segment_pending_records(conn) if r["queue"] != QUEUE_READY_FOR_APPROVAL]
    blockers.sort(key=lambda r: (-r["missing_core_fields"], r["extraction_confidence"] or 0.0))
    return blockers[:limit]
