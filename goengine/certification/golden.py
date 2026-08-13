"""Module 3 -- Golden Dataset Workbench (data layer).

Governance rule: human annotations become the official ground truth
benchmark, and no benchmarking happens against synthetic data. The second
half of that rule is structural, not procedural: `golden_documents.document_id`
is a foreign key into the real `documents` table, so a document can only
enter this dataset if it was actually discovered, downloaded and archived
through the real pipeline (Modules 2-4). Phase 1's synthetic sample GOs are
never inserted into `documents` unless a caller deliberately runs them
through `pipeline.ingest_local_file` -- and even then they'd carry a real,
verifiable source URL and SHA256, at which point they are no longer
"synthetic" in any way that matters to this rule.

Annotations follow the same append-only, supersede-on-correction pattern as
`go_fields` (Phase 1): a re-annotation writes a new row and marks the
previous one superseded, so the original human judgement is never lost.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .. import audit
from ..db import utcnow
from . import categorize

# The full annotation domain (matches the golden_annotations CHECK
# constraint). `project_type` is annotation-only: Module 6 benchmarking does
# not score it because no extractor currently produces it.
SCORED_FIELDS = (
    "go_number", "go_date", "department", "subject", "budget", "district", "scheme_name",
)
ALL_GOLDEN_FIELDS = SCORED_FIELDS + ("project_type",)


class GoldenSetError(ValueError):
    pass


@dataclass
class GoldenDocument:
    id: int
    document_id: int
    added_by: str
    added_at: str
    notes: str | None
    file_name: str
    source_name: str
    department_bucket: str | None
    language: str | None
    text_type: str | None
    annotations: dict[str, dict] = field(default_factory=dict)

    @property
    def annotated_fields(self) -> list[str]:
        return [name for name in SCORED_FIELDS if name in self.annotations]

    @property
    def is_complete(self) -> bool:
        """Every scored field has been given an annotation -- a value, or an
        explicit assertion that the field is absent (value=None is still a
        row; a missing row means "not yet looked at")."""
        return set(SCORED_FIELDS) <= set(self.annotations)


def add_to_golden_set(
    conn: sqlite3.Connection, document_id: int, *, added_by: str, notes: str | None = None
) -> int:
    if not added_by:
        raise GoldenSetError("an annotator identity is required to add a document")

    exists = conn.execute("SELECT id FROM documents WHERE id = ?", (document_id,)).fetchone()
    if exists is None:
        raise LookupError(f"no document with id {document_id}")

    already = conn.execute(
        "SELECT id FROM golden_documents WHERE document_id = ?", (document_id,)
    ).fetchone()
    if already is not None:
        return int(already["id"])

    cur = conn.execute(
        "INSERT INTO golden_documents (document_id, added_by, added_at, notes) VALUES (?, ?, ?, ?)",
        (document_id, added_by, utcnow(), notes),
    )
    golden_id = int(cur.lastrowid)
    audit.record(
        conn,
        action="golden.document_added",
        entity_type="golden_document",
        entity_id=golden_id,
        actor=added_by,
        detail={"document_id": document_id, "notes": notes},
    )
    return golden_id


def annotate_field(
    conn: sqlite3.Connection,
    golden_document_id: int,
    field_name: str,
    value: str | None,
    *,
    annotator: str,
    note: str | None = None,
) -> int:
    """Record ground truth for one field. A blank/None value asserts the
    field genuinely does not appear in the document."""
    if field_name not in ALL_GOLDEN_FIELDS:
        raise GoldenSetError(f"unknown field {field_name!r}; expected one of {ALL_GOLDEN_FIELDS}")
    if not annotator:
        raise GoldenSetError("an annotator identity is required")

    exists = conn.execute(
        "SELECT id FROM golden_documents WHERE id = ?", (golden_document_id,)
    ).fetchone()
    if exists is None:
        raise LookupError(f"no golden document with id {golden_document_id}")

    value = value.strip() if value else None
    current = conn.execute(
        """
        SELECT * FROM golden_annotations
         WHERE golden_document_id = ? AND field_name = ? AND superseded_by IS NULL
        """,
        (golden_document_id, field_name),
    ).fetchone()

    if current is not None and current["value"] == value:
        return int(current["id"])

    now = utcnow()
    cur = conn.execute(
        """
        INSERT INTO golden_annotations
            (golden_document_id, field_name, value, annotator, annotated_at, note)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (golden_document_id, field_name, value, annotator, now, note),
    )
    new_id = int(cur.lastrowid)
    if current is not None:
        conn.execute(
            "UPDATE golden_annotations SET superseded_by = ? WHERE id = ?",
            (new_id, int(current["id"])),
        )

    audit.record(
        conn,
        action="golden.annotated",
        entity_type="golden_document",
        entity_id=golden_document_id,
        actor=annotator,
        field_name=field_name,
        before_value=current["value"] if current is not None else None,
        after_value=value,
        detail={"note": note},
    )
    return new_id


def get_annotations(conn: sqlite3.Connection, golden_document_id: int) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT * FROM golden_annotations
         WHERE golden_document_id = ? AND superseded_by IS NULL
         ORDER BY field_name
        """,
        (golden_document_id,),
    ).fetchall()
    return {r["field_name"]: r for r in rows}


def get_golden_document(conn: sqlite3.Connection, golden_document_id: int) -> GoldenDocument:
    row = conn.execute(
        """
        SELECT g.*, d.file_name, d.id AS document_id, s.name AS source_name,
               c.department_bucket, c.language, c.text_type
          FROM golden_documents g
          JOIN documents d ON d.id = g.document_id
          JOIN sources s ON s.id = d.source_id
          LEFT JOIN document_categories c ON c.document_id = d.id
         WHERE g.id = ?
        """,
        (golden_document_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no golden document with id {golden_document_id}")

    annotations = {
        name: {"value": r["value"], "annotator": r["annotator"], "annotated_at": r["annotated_at"], "note": r["note"]}
        for name, r in get_annotations(conn, golden_document_id).items()
    }
    return GoldenDocument(
        id=int(row["id"]), document_id=int(row["document_id"]), added_by=row["added_by"],
        added_at=row["added_at"], notes=row["notes"], file_name=row["file_name"],
        source_name=row["source_name"], department_bucket=row["department_bucket"],
        language=row["language"], text_type=row["text_type"], annotations=annotations,
    )


def list_golden_documents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT g.id, g.document_id, g.added_by, g.added_at, d.file_name, s.name AS source_name,
               c.department_bucket, c.language, c.text_type,
               (SELECT COUNT(DISTINCT field_name) FROM golden_annotations a
                 WHERE a.golden_document_id = g.id AND a.superseded_by IS NULL) AS annotated_fields
          FROM golden_documents g
          JOIN documents d ON d.id = g.document_id
          JOIN sources s ON s.id = d.source_id
          LEFT JOIN document_categories c ON c.document_id = d.id
         ORDER BY g.id DESC
        """
    ).fetchall()


def candidates_for_golden_set(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Archived, parsed documents not yet in the golden set.

    Ordered to fill the weakest acquisition-program department bucket first,
    so an annotator working through this list naturally balances the
    dataset instead of over-sampling whichever source crawls most easily.
    """
    progress = categorize.acquisition_progress(conn)
    bucket_priority = {
        bucket: info["count"] for bucket, info in progress["departments"].items()
    }

    rows = conn.execute(
        """
        SELECT d.id AS document_id, d.file_name, s.name AS source_name,
               c.department_bucket, c.language, c.text_type
          FROM documents d
          JOIN sources s ON s.id = d.source_id
          JOIN document_categories c ON c.document_id = d.id
         WHERE NOT EXISTS (SELECT 1 FROM golden_documents g WHERE g.document_id = d.id)
         ORDER BY d.id
        """
    ).fetchall()

    def sort_key(row: sqlite3.Row) -> tuple[int, int]:
        bucket = row["department_bucket"]
        # Buckets outside the acquisition program's 4 targets sort last.
        return (bucket_priority.get(bucket, 10_000), int(row["document_id"]))

    return sorted(rows, key=sort_key)[:limit]


def golden_set_summary(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) AS n FROM golden_documents").fetchone()["n"]
    complete = 0
    for row in list_golden_documents(conn):
        if int(row["annotated_fields"]) >= len(SCORED_FIELDS):
            complete += 1
    by_bucket = conn.execute(
        """
        SELECT c.department_bucket, COUNT(*) AS n
          FROM golden_documents g
          JOIN document_categories c ON c.document_id = g.document_id
         GROUP BY c.department_bucket
        """
    ).fetchall()
    return {
        "total": int(total),
        "fully_annotated": complete,
        "by_department_bucket": {r["department_bucket"]: int(r["n"]) for r in by_bucket},
    }
