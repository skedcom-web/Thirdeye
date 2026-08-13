"""Module 3 -- Department Management (master list).

Enable/disable which departments participate, and surface metrics for each.
`bucket_key` links a department to Phase 2's existing categorization
taxonomy (`certification/categorize.py`) where one exists -- departments
without a mapped bucket (Agriculture, Transport, Fisheries, per the
blueprint's own example list) are configurable here but their acquisition
metrics honestly report "not tracked yet" rather than a fabricated number,
since extending the bucket taxonomy is separate work this module doesn't
silently take on.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .. import audit
from ..certification.categorize import ACQUISITION_TARGETS, ALL_BUCKETS
from ..db import utcnow

# Seeded from the blueprint's Module 3 examples. Only the four Phase 2
# already tracks get a bucket_key; the rest are real, configurable rows with
# honestly-reported "not tracked" metrics.
SEED_DEPARTMENTS: tuple[tuple[str, str | None], ...] = (
    ("Health", "health"),
    ("Education", "education"),
    ("Public Works", "public_works"),
    ("Rural Development", "rural_development"),
    ("Agriculture", None),
    ("Transport", None),
    ("Fisheries", None),
)


class DepartmentError(ValueError):
    pass


@dataclass(frozen=True)
class Department:
    id: int
    name: str
    bucket_key: str | None
    active: bool
    publication_status: str


def add_department(
    conn: sqlite3.Connection, *, name: str, bucket_key: str | None = None, actor: str
) -> int:
    if not name.strip():
        raise DepartmentError("department name is required")
    if bucket_key is not None and bucket_key not in ALL_BUCKETS:
        raise DepartmentError(f"bucket_key must be one of {ALL_BUCKETS} or None")

    existing = conn.execute("SELECT id FROM departments WHERE name = ?", (name,)).fetchone()
    if existing is not None:
        return int(existing["id"])

    cur = conn.execute(
        "INSERT INTO departments (name, bucket_key, created_at) VALUES (?, ?, ?)",
        (name.strip(), bucket_key, utcnow()),
    )
    department_id = int(cur.lastrowid)
    audit.record(
        conn, action="department.added", entity_type="department", entity_id=department_id,
        actor=actor, detail={"name": name, "bucket_key": bucket_key},
    )
    return department_id


def set_department_active(
    conn: sqlite3.Connection, department_id: int, active: bool, *, actor: str
) -> None:
    row = conn.execute("SELECT active FROM departments WHERE id = ?", (department_id,)).fetchone()
    if row is None:
        raise LookupError(f"no department with id {department_id}")
    conn.execute("UPDATE departments SET active = ? WHERE id = ?", (1 if active else 0, department_id))
    audit.record(
        conn, action="department.activated" if active else "department.deactivated",
        entity_type="department", entity_id=department_id, actor=actor,
        field_name="active", before_value=bool(row["active"]), after_value=active,
    )


def get_department(conn: sqlite3.Connection, department_id: int) -> Department | None:
    row = conn.execute("SELECT * FROM departments WHERE id = ?", (department_id,)).fetchone()
    return _row_to_department(row) if row else None


def list_departments(conn: sqlite3.Connection) -> list[Department]:
    return [_row_to_department(r) for r in conn.execute("SELECT * FROM departments ORDER BY name").fetchall()]


def _row_to_department(row: sqlite3.Row) -> Department:
    return Department(
        id=int(row["id"]), name=row["name"], bucket_key=row["bucket_key"],
        active=bool(row["active"]), publication_status=row["publication_status"],
    )


def department_metrics(conn: sqlite3.Connection, department: Department) -> dict:
    """Real numbers where Phase 2 tracks this department's bucket; an honest
    'not tracked' marker otherwise."""
    if department.bucket_key is None:
        return {"tracked": False, "document_count": None, "target": None, "accuracy": None}

    document_count = conn.execute(
        "SELECT COUNT(*) AS n FROM document_categories WHERE department_bucket = ?",
        (department.bucket_key,),
    ).fetchone()["n"]

    latest_run = conn.execute(
        "SELECT summary FROM certification_benchmark_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    accuracy = None
    if latest_run is not None:
        import json

        summary = json.loads(latest_run["summary"])
        dept_stats = summary.get("by_department", {}).get(department.bucket_key)
        if dept_stats:
            accuracies = [s["accuracy"] for s in dept_stats.values() if s.get("accuracy") is not None]
            accuracy = round(sum(accuracies) / len(accuracies), 4) if accuracies else None

    return {
        "tracked": True,
        "document_count": int(document_count),
        "target": ACQUISITION_TARGETS.get(department.bucket_key),
        "accuracy": accuracy,
    }


def seed(conn: sqlite3.Connection, *, actor: str) -> list[int]:
    return [add_department(conn, name=name, bucket_key=bucket, actor=actor) for name, bucket in SEED_DEPARTMENTS]
