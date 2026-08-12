"""Module 7 (data layer) -- verification decisions and corrections.

Approve / reject / correct, each writing an audit entry with before and after
values. Corrections never overwrite: the machine-extracted row is retained and
marked superseded, so "what did the extractor originally say" stays answerable
after a human has edited the record.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import audit
from .db import utcnow
from .discovery import crawler
from .extraction.metadata import ALL_FIELDS, CORE_FIELDS

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


class ReviewError(RuntimeError):
    pass


@dataclass
class RecordSummary:
    record_id: int
    document_id: int
    extraction_id: int
    status: str
    file_name: str
    source_name: str
    source_url: str
    sha256: str
    page_count: int
    extraction_confidence: float
    needs_ocr: bool
    fields: dict[str, dict[str, Any]]
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None

    @property
    def missing_core_fields(self) -> list[str]:
        return [name for name in CORE_FIELDS if name not in self.fields]

    @property
    def lowest_confidence(self) -> float:
        if not self.fields:
            return 0.0
        return min(f["confidence"] for f in self.fields.values())


def _require_record(conn: sqlite3.Connection, record_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM go_records WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        raise LookupError(f"no GO record with id {record_id}")
    return row


def get_summary(conn: sqlite3.Connection, record_id: int) -> RecordSummary:
    row = conn.execute(
        """
        SELECT r.*, d.file_name, d.sha256, d.source_url,
               s.name AS source_name,
               e.page_count, e.confidence AS extraction_confidence, e.needs_ocr
          FROM go_records r
          JOIN documents  d ON d.id = r.document_id
          JOIN sources    s ON s.id = r.source_id
          JOIN extractions e ON e.id = r.extraction_id
         WHERE r.id = ?
        """,
        (record_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no GO record with id {record_id}")

    field_rows = conn.execute(
        """
        SELECT * FROM go_fields
         WHERE record_id = ? AND superseded_by IS NULL
         ORDER BY field_name
        """,
        (record_id,),
    ).fetchall()

    fields = {
        r["field_name"]: {
            "id": int(r["id"]),
            "value": r["value"],
            "normalized_value": r["normalized_value"],
            "source_page": int(r["source_page"]),
            "source_text": r["source_text"],
            "confidence": float(r["confidence"]),
            "method": r["method"],
            "origin": r["origin"],
            "created_by": r["created_by"],
        }
        for r in field_rows
    }

    return RecordSummary(
        record_id=int(row["id"]),
        document_id=int(row["document_id"]),
        extraction_id=int(row["extraction_id"]),
        status=row["status"],
        file_name=row["file_name"],
        source_name=row["source_name"],
        source_url=row["source_url"],
        sha256=row["sha256"],
        page_count=int(row["page_count"]),
        extraction_confidence=float(row["extraction_confidence"]),
        needs_ocr=bool(row["needs_ocr"]),
        fields=fields,
        reviewed_by=row["reviewed_by"],
        reviewed_at=row["reviewed_at"],
        review_note=row["review_note"],
    )


def queue(
    conn: sqlite3.Connection, *, status: str = STATUS_PENDING, limit: int = 100
) -> list[sqlite3.Row]:
    """Review queue, lowest-confidence first -- the records most likely to
    need a human are the ones a reviewer should see first."""
    return conn.execute(
        """
        SELECT r.id, r.status, r.created_at, d.file_name, s.name AS source_name,
               e.confidence AS extraction_confidence, e.needs_ocr,
               (SELECT MIN(confidence) FROM go_fields f
                 WHERE f.record_id = r.id AND f.superseded_by IS NULL) AS min_field_confidence,
               (SELECT COUNT(*) FROM go_fields f
                 WHERE f.record_id = r.id AND f.superseded_by IS NULL) AS field_count
          FROM go_records r
          JOIN documents   d ON d.id = r.document_id
          JOIN sources     s ON s.id = r.source_id
          JOIN extractions e ON e.id = r.extraction_id
         WHERE r.status = ?
         ORDER BY COALESCE(min_field_confidence, 0) ASC, r.id ASC
         LIMIT ?
        """,
        (status, limit),
    ).fetchall()


def correct_field(
    conn: sqlite3.Connection,
    record_id: int,
    field_name: str,
    new_value: str,
    *,
    reviewer: str,
    source_page: int | None = None,
    source_text: str | None = None,
    note: str | None = None,
) -> int:
    """Record a human correction as a new, superseding field row.

    A corrected field still needs evidence. If the reviewer does not supply a
    page, the one from the superseded row is carried over; for a field the
    extractor never found, a page is required.
    """
    _require_record(conn, record_id)
    if field_name not in ALL_FIELDS:
        raise ReviewError(f"unknown field {field_name!r}; expected one of {ALL_FIELDS}")
    if not reviewer:
        raise ReviewError("a reviewer identity is required for corrections")

    current = conn.execute(
        """
        SELECT * FROM go_fields
         WHERE record_id = ? AND field_name = ? AND superseded_by IS NULL
        """,
        (record_id, field_name),
    ).fetchone()

    if current is None and source_page is None:
        raise ReviewError(
            f"{field_name} was not extracted, so a source page is required to add it"
        )

    page = source_page if source_page is not None else int(current["source_page"])
    evidence = source_text or (
        f"Corrected by {reviewer}" if current is None else current["source_text"]
    )
    before = current["normalized_value"] if current is not None else None

    if before == new_value:
        return int(current["id"])  # nothing to change

    cur = conn.execute(
        """
        INSERT INTO go_fields
            (record_id, field_name, value, normalized_value, source_page, source_text,
             char_start, char_end, confidence, method, origin, created_at, created_by)
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 1.0, 'human_correction', 'corrected', ?, ?)
        """,
        (record_id, field_name, new_value, new_value, page, evidence, utcnow(), reviewer),
    )
    new_id = int(cur.lastrowid)

    if current is not None:
        conn.execute(
            "UPDATE go_fields SET superseded_by = ? WHERE id = ?", (new_id, int(current["id"]))
        )

    audit.record(
        conn,
        action="field.corrected",
        entity_type="go_record",
        entity_id=record_id,
        actor=reviewer,
        field_name=field_name,
        before_value=before,
        after_value=new_value,
        detail={
            "superseded_field_id": int(current["id"]) if current is not None else None,
            "new_field_id": new_id,
            "source_page": page,
            "note": note,
        },
    )
    return new_id


def approve(
    conn: sqlite3.Connection,
    record_id: int,
    *,
    reviewer: str,
    note: str | None = None,
    allow_missing_fields: bool = False,
) -> None:
    """Approve a record for publication.

    Refuses when a core field is missing unless explicitly overridden: an
    approved record with no GO number is exactly the kind of unverifiable
    entry Phase 1 exists to prevent.
    """
    row = _require_record(conn, record_id)
    if not reviewer:
        raise ReviewError("a reviewer identity is required to approve")

    summary = get_summary(conn, record_id)
    missing = summary.missing_core_fields
    if missing and not allow_missing_fields:
        raise ReviewError(
            "cannot approve with missing core fields: "
            + ", ".join(missing)
            + " -- correct them first, or approve with the override"
        )

    before = row["status"]
    conn.execute(
        "UPDATE go_records SET status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ? WHERE id = ?",
        (STATUS_APPROVED, reviewer, utcnow(), note, record_id),
    )
    _sync_discovered_status(conn, record_id, crawler.STATUS_VERIFIED, reviewer, note)

    audit.record(
        conn,
        action="record.approved",
        entity_type="go_record",
        entity_id=record_id,
        actor=reviewer,
        field_name="status",
        before_value=before,
        after_value=STATUS_APPROVED,
        detail={
            "note": note,
            "missing_core_fields": missing or None,
            "override_used": bool(missing and allow_missing_fields),
        },
    )


def reject(
    conn: sqlite3.Connection, record_id: int, *, reviewer: str, reason: str
) -> None:
    row = _require_record(conn, record_id)
    if not reviewer:
        raise ReviewError("a reviewer identity is required to reject")
    if not reason:
        raise ReviewError("a rejection reason is required")

    before = row["status"]
    conn.execute(
        "UPDATE go_records SET status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ? WHERE id = ?",
        (STATUS_REJECTED, reviewer, utcnow(), reason, record_id),
    )
    _sync_discovered_status(conn, record_id, crawler.STATUS_REJECTED, reviewer, reason)

    audit.record(
        conn,
        action="record.rejected",
        entity_type="go_record",
        entity_id=record_id,
        actor=reviewer,
        field_name="status",
        before_value=before,
        after_value=STATUS_REJECTED,
        detail={"reason": reason},
    )


def _sync_discovered_status(
    conn: sqlite3.Connection, record_id: int, status: str, actor: str, reason: str | None
) -> None:
    row = conn.execute(
        """
        SELECT d.discovered_id
          FROM go_records r JOIN documents d ON d.id = r.document_id
         WHERE r.id = ?
        """,
        (record_id,),
    ).fetchone()
    if row is not None:
        crawler.set_status(
            conn, int(row["discovered_id"]), status, reason=reason, actor=actor
        )


def verified_records(conn: sqlite3.Connection, limit: int = 500) -> list[dict[str, Any]]:
    """The Verified GO Database: approved records only, with their evidence.

    This is the only view downstream phases may consume.
    """
    rows = conn.execute(
        """
        SELECT r.id FROM go_records r
         WHERE r.status = 'approved'
         ORDER BY r.id
         LIMIT ?
        """,
        (limit,),
    ).fetchall()

    output: list[dict[str, Any]] = []
    for row in rows:
        summary = get_summary(conn, int(row["id"]))
        output.append(
            {
                "record_id": summary.record_id,
                "source": summary.source_name,
                "source_url": summary.source_url,
                "file_name": summary.file_name,
                "sha256": summary.sha256,
                "reviewed_by": summary.reviewed_by,
                "reviewed_at": summary.reviewed_at,
                "fields": {
                    name: {
                        "value": data["normalized_value"],
                        "source_page": data["source_page"],
                        "source_text": data["source_text"],
                        "confidence": data["confidence"],
                        "origin": data["origin"],
                    }
                    for name, data in summary.fields.items()
                },
            }
        )
    return output


def counts_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM go_records GROUP BY status"
    ).fetchall()
    counts = {STATUS_PENDING: 0, STATUS_APPROVED: 0, STATUS_REJECTED: 0}
    for row in rows:
        counts[row["status"]] = int(row["n"])
    return counts
