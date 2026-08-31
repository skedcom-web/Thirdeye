"""Extraction quality metrics -- Next Phase Blueprint's quality dashboard.

None of these existed before: this module defines each metric precisely
against real schema/vocabulary already in use elsewhere (extraction.metadata's
CORE_FIELDS/ALL_FIELDS, extractions.needs_ocr, audit_log), rather than
reinventing a parallel notion of "quality." Every number here is computed
fresh from current data -- nothing is cached or estimated.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .. import repository
from ..config import Settings
from ..extraction.metadata import ALL_FIELDS, CORE_FIELDS, OPTIONAL_FIELDS

_RECENT_WINDOW_DAYS = 30


def _pct(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 1) if denominator else 0.0


# ---------------------------------------------------------------------------
# Phase 3.5, Initiative 2 -- GO Quality Scoring Engine
#
# The blueprint names 7 criteria but not weights -- this is a defensible
# default, documented here rather than buried in a commit, so it can be
# revisited: CORE_FIELDS (go_number/go_date/department/subject) carry the
# most weight since they're what makes a record usable at all, PDF
# availability and OCR confidence reflect whether the evidence itself is
# trustworthy, and metadata completeness (the 3 OPTIONAL_FIELDS) is a
# smaller bonus since its absence is often just "not applicable to this GO."
QUALITY_WEIGHTS: dict[str, float] = {
    "go_number": 20,
    "go_date": 15,
    "department": 15,
    "subject": 15,
    "pdf_availability": 15,
    "ocr_confidence": 10,
    "metadata_completeness": 10,
}
assert sum(QUALITY_WEIGHTS.values()) == 100

CATEGORY_EXCELLENT = "Excellent"
CATEGORY_GOOD = "Good"
CATEGORY_NEEDS_REVIEW = "Needs Review"
CATEGORY_POOR = "Poor"
CATEGORY_NO_DATA = "No Data"


def quality_category(score: float) -> str:
    if score >= 90:
        return CATEGORY_EXCELLENT
    if score >= 75:
        return CATEGORY_GOOD
    if score >= 50:
        return CATEGORY_NEEDS_REVIEW
    return CATEGORY_POOR


def _score_from_parts(
    present_fields: set[str], extraction_confidence: float, pdf_available: bool
) -> tuple[float, dict[str, float]]:
    breakdown: dict[str, float] = {}
    score = 0.0
    for field_name in CORE_FIELDS:
        earned = QUALITY_WEIGHTS[field_name] if field_name in present_fields else 0
        breakdown[field_name] = earned
        score += earned

    earned = QUALITY_WEIGHTS["pdf_availability"] if pdf_available else 0
    breakdown["pdf_availability"] = earned
    score += earned

    ocr_points = round(min(max(extraction_confidence, 0.0), 1.0) * QUALITY_WEIGHTS["ocr_confidence"], 1)
    breakdown["ocr_confidence"] = ocr_points
    score += ocr_points

    optional_present = sum(1 for f in OPTIONAL_FIELDS if f in present_fields)
    metadata_points = round(optional_present / len(OPTIONAL_FIELDS) * QUALITY_WEIGHTS["metadata_completeness"], 1)
    breakdown["metadata_completeness"] = metadata_points
    score += metadata_points

    return round(score, 1), breakdown


def go_quality_score(conn: sqlite3.Connection, settings: Settings, record_id: int) -> dict:
    """Single-record score, 0-100, plus its category and per-criterion
    breakdown -- reusable from record.html, not just the bulk health table."""
    row = conn.execute(
        """
        SELECT r.document_id, e.confidence AS extraction_confidence
          FROM go_records r JOIN extractions e ON e.id = r.extraction_id
         WHERE r.id = ?
        """,
        (record_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no go_records row with id {record_id}")

    present = {
        f["field_name"] for f in conn.execute(
            "SELECT field_name FROM go_fields WHERE record_id = ? AND superseded_by IS NULL", (record_id,)
        ).fetchall()
    }
    pdf_ok = repository.is_available(settings, conn, int(row["document_id"]))
    score, breakdown = _score_from_parts(present, float(row["extraction_confidence"] or 0.0), pdf_ok)
    return {"record_id": record_id, "score": score, "category": quality_category(score), "breakdown": breakdown}


def _bulk_quality_rows(
    conn: sqlite3.Connection, settings: Settings, *, department: str | None = None
) -> list[dict]:
    where = "WHERE s.department = ?" if department else ""
    params: list = [department] if department else []
    base = conn.execute(
        f"""
        SELECT r.id AS record_id, r.document_id, s.department AS department,
               e.confidence AS extraction_confidence
          FROM go_records r
          JOIN sources s ON s.id = r.source_id
          JOIN extractions e ON e.id = r.extraction_id
          {where}
        """,
        params,
    ).fetchall()

    fields_by_record: dict[int, set[str]] = {}
    for row in conn.execute("SELECT record_id, field_name FROM go_fields WHERE superseded_by IS NULL").fetchall():
        fields_by_record.setdefault(int(row["record_id"]), set()).add(row["field_name"])

    results = []
    for row in base:
        record_id = int(row["record_id"])
        present = fields_by_record.get(record_id, set())
        pdf_ok = repository.is_available(settings, conn, int(row["document_id"]))
        score, breakdown = _score_from_parts(present, float(row["extraction_confidence"] or 0.0), pdf_ok)
        results.append({
            "record_id": record_id,
            "department": row["department"],
            "score": score,
            "category": quality_category(score),
        })
    return results


def department_health(conn: sqlite3.Connection, settings: Settings) -> list[dict]:
    """Phase 3.5 Initiative 1 (+ the living "Department Validation Report"
    of Initiative 7): one row per configured department. A department with
    zero extracted GOs shows status "No Data" rather than a fabricated
    score -- this table only ever reflects real extraction that has
    actually happened."""
    from .. import registry

    scores_by_department: dict[str, list[float]] = {}
    for row in _bulk_quality_rows(conn, settings):
        scores_by_department.setdefault(row["department"], []).append(row["score"])

    result = []
    for department in registry.list_departments(conn):
        stats = conn.execute(
            """
            SELECT COUNT(*) AS total, MAX(r.created_at) AS last_extraction
              FROM go_records r JOIN sources s ON s.id = r.source_id
             WHERE s.department = ?
            """,
            (department,),
        ).fetchone()
        latest = conn.execute(
            """
            SELECT r.go_identifier, r.go_number_raw FROM go_records r
              JOIN sources s ON s.id = r.source_id
             WHERE s.department = ?
             ORDER BY r.id DESC LIMIT 1
            """,
            (department,),
        ).fetchone()

        dept_scores = scores_by_department.get(department, [])
        total = int(stats["total"])
        avg_score = round(sum(dept_scores) / len(dept_scores), 1) if dept_scores else None
        status = CATEGORY_NO_DATA if total == 0 else quality_category(avg_score)

        result.append({
            "department": department,
            "total_gos": total,
            "latest_go": (latest["go_identifier"] or latest["go_number_raw"]) if latest else None,
            "last_extraction_date": stats["last_extraction"],
            "quality_score": avg_score,
            "status": status,
        })
    return result


def department_coverage_kpis(conn: sqlite3.Connection, departments: list[str], health: list[dict]) -> dict:
    """The KPI row above the Department Health Table. Takes the already-
    computed department list/health rows rather than recomputing them, so a
    page rendering both never pays for the scoring pass twice."""
    extracted = sum(1 for row in health if row["total_gos"] > 0)
    requiring_attention = sum(1 for row in health if row["status"] in (CATEGORY_NO_DATA, CATEGORY_POOR))
    last_extraction = conn.execute(
        "SELECT MAX(finished_at) AS ts FROM extraction_requests WHERE status = 'COMPLETED'"
    ).fetchone()["ts"]
    return {
        "configured": len(departments),
        "extracted": extracted,
        "requiring_attention": requiring_attention,
        "last_successful_extraction": last_extraction,
    }


def _records_with_all_fields_present(
    conn: sqlite3.Connection, fields: tuple[str, ...], *, needs_ocr: bool | None = None
) -> int:
    """Count of go_records that have a current (non-superseded) go_fields
    row for every field in `fields`. Optionally scoped to records whose
    extraction did/didn't need OCR."""
    placeholders = ",".join("?" for _ in fields)
    sql = f"""
        SELECT COUNT(*) AS n FROM (
            SELECT r.id
              FROM go_records r
              JOIN go_fields f ON f.record_id = r.id AND f.superseded_by IS NULL AND f.field_name IN ({placeholders})
    """
    params: list = list(fields)
    if needs_ocr is not None:
        sql += " JOIN extractions e ON e.id = r.extraction_id AND e.needs_ocr = ?"
        params.append(1 if needs_ocr else 0)
    sql += " GROUP BY r.id HAVING COUNT(DISTINCT f.field_name) = ? )"
    params.append(len(fields))
    return conn.execute(sql, params).fetchone()["n"]


def extraction_success_rate(conn: sqlite3.Connection) -> dict:
    """% of go_records with every core field present -- the extractor's
    actual, evidence-backed success rate, not just "a record got created"."""
    total = conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"]
    successful = _records_with_all_fields_present(conn, CORE_FIELDS)
    return {"total": total, "successful": successful, "rate": _pct(successful, total)}


def ocr_recovery_rate(conn: sqlite3.Connection) -> dict:
    """Of extractions that needed OCR, % whose resulting record still ended
    up with every core field present -- did OCR actually recover usable
    metadata, not just "did OCR run"."""
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM go_records r JOIN extractions e ON e.id = r.extraction_id WHERE e.needs_ocr = 1"
    ).fetchone()["n"]
    recovered = _records_with_all_fields_present(conn, CORE_FIELDS, needs_ocr=True)
    return {"total": total, "recovered": recovered, "rate": _pct(recovered, total)}


def missing_metadata(conn: sqlite3.Connection) -> list[dict]:
    """Per-field breakdown of how many go_records lack that field -- not a
    single number, since "missing budget" and "missing go_number" mean very
    different things for a reviewer to act on."""
    total = conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"]
    rows = []
    for field_name in ALL_FIELDS:
        present = conn.execute(
            """
            SELECT COUNT(*) AS n FROM go_records r
             WHERE EXISTS (
                SELECT 1 FROM go_fields f
                 WHERE f.record_id = r.id AND f.field_name = ? AND f.superseded_by IS NULL
             )
            """,
            (field_name,),
        ).fetchone()["n"]
        missing = total - present
        rows.append({
            "field_name": field_name,
            "is_core": field_name in CORE_FIELDS,
            "missing": missing,
            "rate": _pct(missing, total),
        })
    return rows


def review_corrections(conn: sqlite3.Connection) -> dict:
    """How much reviewer correction the extractor's output actually needs,
    per field -- audit_log has no aggregation helper today (audit.py stays
    a generic trail, not metrics-aware), so this is fresh SQL."""
    by_field = conn.execute(
        """
        SELECT field_name, COUNT(*) AS n FROM audit_log
         WHERE action = 'field.corrected'
         GROUP BY field_name
         ORDER BY n DESC
        """
    ).fetchall()
    threshold = (datetime.now(timezone.utc) - timedelta(days=_RECENT_WINDOW_DAYS)).isoformat(timespec="seconds")
    recent = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'field.corrected' AND ts >= ?", (threshold,)
    ).fetchone()["n"]
    return {
        "by_field": [{"field_name": r["field_name"], "count": r["n"]} for r in by_field],
        "total": sum(r["n"] for r in by_field),
        "recent_days": _RECENT_WINDOW_DAYS,
        "recent_count": recent,
    }


def department_coverage(conn: sqlite3.Connection) -> dict:
    """% of configured departments (sources.department, now up to 38 after
    the Next Phase Blueprint's expansion) with at least one approved
    go_records row. Deliberately separate from operations/publication.py's
    publication_coverage(), which counts an unrelated, much smaller
    `departments` master table -- not touched here."""
    total = conn.execute(
        "SELECT COUNT(DISTINCT department) AS n FROM sources WHERE active = 1"
    ).fetchone()["n"]
    covered = conn.execute(
        """
        SELECT COUNT(DISTINCT s.department) AS n
          FROM sources s
          JOIN go_records r ON r.source_id = s.id
         WHERE s.active = 1 AND r.status = 'approved'
        """
    ).fetchone()["n"]
    return {"total": total, "covered": covered, "rate": _pct(covered, total)}


# ---------------------------------------------------------------------------
# Phase 3.5, Initiative 3 -- Missing Metadata Workbench
#
# Deliberately distinct from operations/review.py's existing QUEUE_METADATA,
# which means *low field confidence* (a value was extracted but the
# extractor wasn't sure of it) -- this queue is about a field being entirely
# ABSENT, a different problem needing a different fix (correct/reprocess,
# not just "double-check this weak value").
# ---------------------------------------------------------------------------
def missing_metadata_queue(
    conn: sqlite3.Connection, settings: Settings, *, department: str | None = None, limit: int = 200
) -> list[dict]:
    """Pending records missing a core field and/or their PDF -- approved and
    rejected records are already decided and out of scope for this queue."""
    where = "WHERE r.status = 'pending'"
    params: list = []
    if department:
        where += " AND s.department = ?"
        params.append(department)
    params.append(limit)

    candidates = conn.execute(
        f"""
        SELECT r.id AS record_id, r.document_id, r.go_identifier, s.department,
               s.name AS source_name, d.file_name
          FROM go_records r
          JOIN sources s ON s.id = r.source_id
          JOIN documents d ON d.id = r.document_id
          {where}
         ORDER BY r.id
         LIMIT ?
        """,
        params,
    ).fetchall()

    fields_by_record: dict[int, set[str]] = {}
    for row in conn.execute("SELECT record_id, field_name FROM go_fields WHERE superseded_by IS NULL").fetchall():
        fields_by_record.setdefault(int(row["record_id"]), set()).add(row["field_name"])

    result = []
    for row in candidates:
        record_id = int(row["record_id"])
        present = fields_by_record.get(record_id, set())
        missing_fields = [f for f in CORE_FIELDS if f not in present]
        pdf_missing = not repository.is_available(settings, conn, int(row["document_id"]))
        if not missing_fields and not pdf_missing:
            continue
        result.append({
            "record_id": record_id,
            "go_identifier": row["go_identifier"],
            "department": row["department"],
            "source_name": row["source_name"],
            "file_name": row["file_name"],
            "missing_fields": missing_fields,
            "missing_pdf": pdf_missing,
        })
    return result


def quality_summary(conn: sqlite3.Connection) -> dict:
    return {
        "extraction_success": extraction_success_rate(conn),
        "ocr_recovery": ocr_recovery_rate(conn),
        "missing_metadata": missing_metadata(conn),
        "review_corrections": review_corrections(conn),
        "department_coverage": department_coverage(conn),
    }
