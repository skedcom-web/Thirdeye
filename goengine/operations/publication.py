"""Module 8 -- Publication Control Center.

    No publication without certification.
    No publication without approval.
    No publication without provenance.

All three are checked in code, every time -- there is no "already
published, skip re-checking" shortcut, and no direct write path to
`publication_status` that bypasses these functions.

Provenance is structurally guaranteed by the Phase 1 schema (every
`go_fields` row requires a source page and source text; nothing without
those can exist), so the provenance check here reduces to "does at least
one properly-evidenced approved record exist" -- the same fact the approval
check already establishes, made explicit rather than silently assumed.

"Publish Project" from the blueprint is deliberately not implemented: no
Project entity exists until Phase 4 (Geography Intelligence), and inventing
one here would be scope creep this phase doesn't need.
"""

from __future__ import annotations

import sqlite3

from .. import audit
from ..db import utcnow
from . import geography

CERTIFIED = "CERTIFIED"


class PublicationError(ValueError):
    pass


# ---------------------------------------------------------------------------
# District publication
# ---------------------------------------------------------------------------
def publish_district(conn: sqlite3.Connection, district_id: int, *, actor: str) -> None:
    district = geography.get_district(conn, district_id)
    if district is None:
        raise LookupError(f"no district with id {district_id}")

    if district.certification_status != CERTIFIED:
        raise PublicationError(
            f"district is not certified (status: {district.certification_status}); "
            "run certification against its sources first"
        )

    approved_count = conn.execute(
        "SELECT COUNT(*) AS n FROM go_records WHERE status = 'approved'"
    ).fetchone()["n"]
    if approved_count == 0:
        raise PublicationError("no approved, evidenced records exist yet -- nothing to publish")

    now = utcnow()
    conn.execute(
        "UPDATE districts SET publication_status = 'PUBLISHED', status = 'PUBLISHED', published_by = ?, published_at = ? WHERE id = ?",
        (actor, now, district_id),
    )
    audit.record(
        conn, action="district.published", entity_type="district", entity_id=district_id, actor=actor,
        field_name="publication_status", before_value="NOT_PUBLISHED", after_value="PUBLISHED",
        detail={"approved_records_at_publish": approved_count},
    )


def unpublish_district(conn: sqlite3.Connection, district_id: int, *, actor: str, reason: str) -> None:
    district = geography.get_district(conn, district_id)
    if district is None:
        raise LookupError(f"no district with id {district_id}")
    if not reason:
        raise PublicationError("a reason is required to unpublish")

    conn.execute(
        "UPDATE districts SET publication_status = 'NOT_PUBLISHED', status = 'CERTIFIED' WHERE id = ?",
        (district_id,),
    )
    audit.record(
        conn, action="district.unpublished", entity_type="district", entity_id=district_id, actor=actor,
        field_name="publication_status", before_value="PUBLISHED", after_value="NOT_PUBLISHED",
        detail={"reason": reason},
    )


# ---------------------------------------------------------------------------
# Department publication
# ---------------------------------------------------------------------------
def _department_is_certified(conn: sqlite3.Connection, bucket_key: str) -> bool:
    """True if a CERTIFIED source has actually produced a document bucketed
    into this department.

    Deliberately NOT "does a certified source's registered department name
    map to this bucket": a general-purpose portal is often registered under
    a broad name like "All Departments", which maps to no specific bucket at
    all even though the documents it produces get bucketed per-document
    using the smarter extracted-department signal (categorize.py). Checking
    through document_categories keeps this consistent with how every other
    department metric in the app is computed, and ties "certified" to real
    evidence rather than a source-level name heuristic.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
          FROM document_categories c
          JOIN documents d ON d.id = c.document_id
          JOIN sources s ON s.id = d.source_id
         WHERE c.department_bucket = ? AND s.certification_status = 'CERTIFIED'
        """,
        (bucket_key,),
    ).fetchone()
    return row["n"] > 0


def publish_department(conn: sqlite3.Connection, department_id: int, *, actor: str) -> None:
    row = conn.execute("SELECT * FROM departments WHERE id = ?", (department_id,)).fetchone()
    if row is None:
        raise LookupError(f"no department with id {department_id}")
    bucket_key = row["bucket_key"]
    if bucket_key is None:
        raise PublicationError(
            f"{row['name']} has no acquisition bucket mapped -- nothing is tracked for it to publish"
        )

    if not _department_is_certified(conn, bucket_key):
        raise PublicationError(f"no certified source covers the {row['name']} department yet")

    approved_count = conn.execute(
        """
        SELECT COUNT(*) AS n FROM go_records r
          JOIN documents d ON d.id = r.document_id
          JOIN document_categories c ON c.document_id = d.id
         WHERE r.status = 'approved' AND c.department_bucket = ?
        """,
        (bucket_key,),
    ).fetchone()["n"]
    if approved_count == 0:
        raise PublicationError(f"no approved records exist yet for {row['name']}")

    conn.execute(
        "UPDATE departments SET publication_status = 'PUBLISHED', published_by = ?, published_at = ? WHERE id = ?",
        (actor, utcnow(), department_id),
    )
    audit.record(
        conn, action="department.published", entity_type="department", entity_id=department_id, actor=actor,
        field_name="publication_status", before_value="NOT_PUBLISHED", after_value="PUBLISHED",
        detail={"approved_records_at_publish": approved_count},
    )


def unpublish_department(conn: sqlite3.Connection, department_id: int, *, actor: str, reason: str) -> None:
    row = conn.execute("SELECT id FROM departments WHERE id = ?", (department_id,)).fetchone()
    if row is None:
        raise LookupError(f"no department with id {department_id}")
    if not reason:
        raise PublicationError("a reason is required to unpublish")

    conn.execute("UPDATE departments SET publication_status = 'NOT_PUBLISHED' WHERE id = ?", (department_id,))
    audit.record(
        conn, action="department.unpublished", entity_type="department", entity_id=department_id, actor=actor,
        field_name="publication_status", before_value="PUBLISHED", after_value="NOT_PUBLISHED",
        detail={"reason": reason},
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def publication_coverage(conn: sqlite3.Connection) -> dict:
    districts = conn.execute("SELECT COUNT(*) AS n FROM districts").fetchone()["n"]
    published_districts = conn.execute(
        "SELECT COUNT(*) AS n FROM districts WHERE publication_status = 'PUBLISHED'"
    ).fetchone()["n"]
    departments = conn.execute("SELECT COUNT(*) AS n FROM departments WHERE active = 1").fetchone()["n"]
    published_departments = conn.execute(
        "SELECT COUNT(*) AS n FROM departments WHERE publication_status = 'PUBLISHED'"
    ).fetchone()["n"]
    return {
        "districts_total": int(districts),
        "districts_published": int(published_districts),
        "departments_total": int(departments),
        "departments_published": int(published_departments),
    }
