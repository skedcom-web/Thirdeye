"""Detects duplicate PENDING go_records for the same document -- the
fingerprint left behind by retrying a sync before pipeline.ingest_document_
bytes was fixed to stop re-parsing unchanged content (see that module's
docstring). Read-only: this module only reports what's duplicated. Removing
anything is a deliberate, separate, explicitly-approved step -- never
triggered from here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


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
