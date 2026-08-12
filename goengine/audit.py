"""Module 8 -- Audit & Traceability Engine.

Every state change in the pipeline funnels through `record()`. The table is
append-only at the database level (see the triggers in schema.sql), so an
audit entry cannot be quietly rewritten after the fact.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .db import utcnow

SYSTEM_ACTOR = "system"


@dataclass(frozen=True)
class AuditEntry:
    id: int
    ts: str
    actor: str
    action: str
    entity_type: str
    entity_id: int | None
    field_name: str | None
    before_value: str | None
    after_value: str | None
    detail: dict[str, Any] | None


def record(
    conn: sqlite3.Connection,
    *,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    actor: str = SYSTEM_ACTOR,
    field_name: str | None = None,
    before_value: Any = None,
    after_value: Any = None,
    detail: dict[str, Any] | None = None,
) -> int:
    """Append one audit entry. Returns its id."""
    cur = conn.execute(
        """
        INSERT INTO audit_log
            (ts, actor, action, entity_type, entity_id, field_name,
             before_value, after_value, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utcnow(),
            actor,
            action,
            entity_type,
            entity_id,
            field_name,
            _stringify(before_value),
            _stringify(after_value),
            json.dumps(detail, ensure_ascii=False) if detail else None,
        ),
    )
    return int(cur.lastrowid)


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def trail(
    conn: sqlite3.Connection,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    limit: int = 200,
) -> list[AuditEntry]:
    """Read the audit trail, newest first, optionally scoped to one entity."""
    clauses: list[str] = []
    params: list[Any] = []
    if entity_type is not None:
        clauses.append("entity_type = ?")
        params.append(entity_type)
    if entity_id is not None:
        clauses.append("entity_id = ?")
        params.append(entity_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?", params
    ).fetchall()
    return [_row_to_entry(r) for r in rows]


def document_provenance(conn: sqlite3.Connection, document_id: int) -> list[AuditEntry]:
    """Full chain for one document: discovery -> download -> parse -> review.

    Walks up to the discovered_document and down to the GO record so the whole
    lineage reads as a single ordered story, which is what an auditor asks for.
    """
    doc = conn.execute(
        "SELECT discovered_id FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if doc is None:
        return []

    record_ids = [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM go_records WHERE document_id = ?", (document_id,)
        ).fetchall()
    ]
    extraction_ids = [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM extractions WHERE document_id = ?", (document_id,)
        ).fetchall()
    ]

    targets: list[tuple[str, int]] = [
        ("discovered_document", int(doc["discovered_id"])),
        ("document", document_id),
    ]
    targets += [("extraction", i) for i in extraction_ids]
    targets += [("go_record", i) for i in record_ids]

    entries: list[AuditEntry] = []
    for entity_type, entity_id in targets:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        ).fetchall()
        entries.extend(_row_to_entry(r) for r in rows)

    entries.sort(key=lambda e: e.id)
    return entries


def _row_to_entry(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        id=int(row["id"]),
        ts=row["ts"],
        actor=row["actor"],
        action=row["action"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        field_name=row["field_name"],
        before_value=row["before_value"],
        after_value=row["after_value"],
        detail=json.loads(row["detail"]) if row["detail"] else None,
    )
