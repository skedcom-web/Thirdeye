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


# ---------------------------------------------------------------------------
# Phase 3.7 Initiative 7 -- Extraction Coverage Dashboard
#
# Composes department_coverage_kpis() rather than recomputing it -- 4 of
# its 5 numbers already exist there. Only success/failure rate are new.
# ---------------------------------------------------------------------------
def extraction_coverage(conn: sqlite3.Connection, kpis: dict) -> dict:
    """`kpis` is department_coverage_kpis()'s own return value -- callers
    that already computed it (every current caller does, to build the
    Department Health Table) pass it straight through rather than paying
    for a second pass over the same department data."""
    configured = kpis["configured"]
    completed = kpis["extracted"]

    request_counts = conn.execute(
        "SELECT status, COUNT(*) AS n FROM extraction_requests GROUP BY status"
    ).fetchall()
    total_requests = sum(r["n"] for r in request_counts)
    failed_requests = sum(r["n"] for r in request_counts if r["status"] == "FAILED")

    return {
        "departments_completed": completed,
        "departments_remaining": configured - completed,
        "success_rate_pct": _pct(completed, configured),
        # Request-level, not per-department -- there is no per-department
        # attempt-tracking today (a department can be scoped by several
        # requests, or none), so this is the only granularity that's real
        # rather than invented for this report.
        "failure_rate_pct": _pct(failed_requests, total_requests),
        "latest_successful_extraction": kpis["last_successful_extraction"],
    }


# ---------------------------------------------------------------------------
# Phase 3.6 Initiative A -- Department Readiness Certification Center
# ---------------------------------------------------------------------------
STATUS_READY = "Ready"
STATUS_PARTIALLY_READY = "Partially Ready"
STATUS_NEEDS_ATTENTION = "Needs Attention"


def department_readiness(conn: sqlite3.Connection, settings: Settings) -> list[dict]:
    """One row per configured department, checked against 6 real,
    independently-verifiable criteria -- reuses department_health()'s
    total_gos rather than recomputing it. A department with zero extracted
    GOs is "Needs Attention" outright (nothing to certify yet); otherwise
    "Ready" only if every check passes, else "Partially Ready"."""
    from .. import public, registry

    health_by_department = {h["department"]: h for h in department_health(conn, settings)}

    result = []
    for department in registry.list_departments(conn):
        total_gos = health_by_department.get(department, {}).get("total_gos", 0)

        records = conn.execute(
            """
            SELECT r.id AS record_id, r.document_id, r.go_url_slug FROM go_records r
              JOIN sources s ON s.id = r.source_id
             WHERE s.department = ?
            """,
            (department,),
        ).fetchall()

        pdf_available = any(repository.is_available(settings, conn, int(r["document_id"])) for r in records)
        permanent_url_available = any(r["go_url_slug"] for r in records)

        metadata_complete = False
        for r in records:
            present = {
                f["field_name"] for f in conn.execute(
                    "SELECT field_name FROM go_fields WHERE record_id = ? AND superseded_by IS NULL",
                    (r["record_id"],),
                ).fetchall()
            }
            if all(field in present for field in CORE_FIELDS):
                metadata_complete = True
                break

        # A real functional check, not a data-shape guess: does searching
        # for this department by name actually surface its own GOs.
        searchable = total_gos > 0 and public.search(conn, q=department)[1] > 0

        checklist = {
            "latest_go_extracted": total_gos > 0,
            "historical_go_available": total_gos >= 2,
            "pdf_available": pdf_available,
            "metadata_complete": metadata_complete,
            "searchable": searchable,
            "permanent_url_available": permanent_url_available,
        }

        if total_gos == 0:
            status = STATUS_NEEDS_ATTENTION
        elif all(checklist.values()):
            status = STATUS_READY
        else:
            status = STATUS_PARTIALLY_READY

        result.append({
            "department": department,
            "total_gos": total_gos,
            "checklist": checklist,
            "status": status,
        })
    return result


# ---------------------------------------------------------------------------
# Phase 3.6 Initiative E -- Publication Confidence Model
#
# Maps the internal Quality Score (Phase 3.5's go_quality_score) into a
# citizen-friendly label. Only the label ever reaches a citizen-facing
# template -- the numeric score and its per-criterion breakdown stay
# server-side, per the directive's "do not expose internal scoring
# calculations." Thresholds are a defensible default (the blueprint names
# the 3 labels but not the cutoffs), documented here so they can be revisited.
# ---------------------------------------------------------------------------
CONFIDENCE_HIGH = "High Confidence"
CONFIDENCE_MEDIUM = "Medium Confidence"
CONFIDENCE_REVIEW_RECOMMENDED = "Review Recommended"

_CONFIDENCE_HIGH_THRESHOLD = 85
_CONFIDENCE_MEDIUM_THRESHOLD = 60


def publication_confidence_label(score: float) -> str:
    if score >= _CONFIDENCE_HIGH_THRESHOLD:
        return CONFIDENCE_HIGH
    if score >= _CONFIDENCE_MEDIUM_THRESHOLD:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_REVIEW_RECOMMENDED


def publication_confidence(conn: sqlite3.Connection, settings: Settings, record_id: int) -> str:
    return publication_confidence_label(go_quality_score(conn, settings, record_id)["score"])


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


# ---------------------------------------------------------------------------
# Phase 3.6 Initiative F -- Repository Health Dashboard
#
# Composes signals already built above rather than reinventing them. Does
# NOT include the "trend" view here: that's Initiative B's by-year
# published counts (operations/analytics.py), which can't be imported from
# this module without an import cycle (analytics.py already imports
# department_coverage/department_health from here) -- the /ops/repository
# route combines the two at render time instead.
# ---------------------------------------------------------------------------
def repository_health(conn: sqlite3.Connection, settings: Settings) -> dict:
    metadata_completeness_pct = extraction_success_rate(conn)["rate"]

    document_ids = [int(r["document_id"]) for r in conn.execute("SELECT DISTINCT document_id FROM go_records").fetchall()]
    pdf_available_count = sum(1 for doc_id in document_ids if repository.is_available(settings, conn, doc_id))
    pdf_availability_pct = _pct(pdf_available_count, len(document_ids))

    readiness = department_readiness(conn, settings)
    departments_with_data = [d for d in readiness if d["total_gos"] > 0]
    search_indexing_coverage_pct = _pct(
        sum(1 for d in departments_with_data if d["checklist"]["searchable"]), len(departments_with_data)
    )
    readiness_counts = {STATUS_READY: 0, STATUS_PARTIALLY_READY: 0, STATUS_NEEDS_ATTENTION: 0}
    for d in readiness:
        readiness_counts[d["status"]] += 1

    confidence_counts = {CONFIDENCE_HIGH: 0, CONFIDENCE_MEDIUM: 0, CONFIDENCE_REVIEW_RECOMMENDED: 0}
    approved_ids = [
        int(r["id"]) for r in conn.execute("SELECT id FROM go_records WHERE status = 'approved'").fetchall()
    ]
    for record_id in approved_ids:
        confidence_counts[publication_confidence(conn, settings, record_id)] += 1

    return {
        "metadata_completeness_pct": metadata_completeness_pct,
        "pdf_availability_pct": pdf_availability_pct,
        "search_indexing_coverage_pct": search_indexing_coverage_pct,
        "department_readiness": readiness_counts,
        "publication_confidence_distribution": confidence_counts,
    }


# ---------------------------------------------------------------------------
# Phase 3.8 Initiatives 1, 3 & 5 -- Department Certification Matrix
#
# A 5-level maturity ladder per department, built entirely from signals that
# already exist (department_health's total_gos, department_readiness's
# Ready/Partially Ready/Needs Attention check) rather than new criteria.
# Levels are ORDERED: a department only reaches a level if every lower one
# also holds.
# ---------------------------------------------------------------------------
CERT_LEVEL_LABELS: dict[int, str] = {
    1: "Reachable",
    2: "Extractable",
    3: "Parsable",
    4: "Publishable",
    5: "Searchable & Production Ready",
}

# Phase 3.9 Initiative 2 -- Publication Yield KPI thresholds.
YIELD_GREEN = "Green"
YIELD_AMBER = "Amber"
YIELD_RED = "Red"
_YIELD_GREEN_THRESHOLD = 70.0
_YIELD_AMBER_THRESHOLD = 40.0


def _yield_status(yield_pct: float) -> str:
    if yield_pct >= _YIELD_GREEN_THRESHOLD:
        return YIELD_GREEN
    if yield_pct >= _YIELD_AMBER_THRESHOLD:
        return YIELD_AMBER
    return YIELD_RED


def _certification_level(
    *, documents_downloaded: int, records_parsed: int, records_approved: int, readiness_status: str
) -> int:
    if readiness_status == STATUS_READY:
        return 5
    if records_approved > 0:
        return 4
    if records_parsed > 0:
        return 3
    if documents_downloaded > 0:
        return 2
    return 1


def department_certification(conn: sqlite3.Connection, settings: Settings) -> list[dict]:
    """One row per real configured department (registry.list_departments),
    combining Initiative 1's certification ladder, Initiative 3's adapter
    info, and Initiative 5's benchmarking KPIs into a single table -- these
    are the same per-department columns, not three separate concerns.

    "Records Approved" and "Records Published" are reported as one number
    (`records_approved`): public.search()'s only citizen-visibility gate is
    go_records.status='approved', so today those are the same signal -- this
    reports one honest number rather than inventing a second, fake one.
    """
    import json

    from .. import registry

    health_by_department = {h["department"]: h for h in department_health(conn, settings)}
    readiness_by_department = {r["department"]: r["status"] for r in department_readiness(conn, settings)}

    # One pass over every request, fanned out to each department it scoped --
    # avoids an N-query loop over ~40 departments (a request can span several).
    requests_by_department: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT department_filter, status, started_at, finished_at FROM extraction_requests"
        " WHERE department_filter IS NOT NULL"
    ).fetchall():
        for department in json.loads(row["department_filter"]):
            requests_by_department.setdefault(department, []).append(row)

    source_rows = {
        r["department"]: r
        for r in conn.execute("SELECT department, url, adapter FROM sources GROUP BY department").fetchall()
    }
    documents_found_by_department = {
        r["department"]: int(r["n"]) for r in conn.execute(
            """
            SELECT s.department AS department, COUNT(*) AS n FROM discovered_documents dd
              JOIN sources s ON s.id = dd.source_id
             GROUP BY s.department
            """
        ).fetchall()
    }
    documents_downloaded_by_department = {
        r["department"]: int(r["n"]) for r in conn.execute(
            """
            SELECT s.department AS department, COUNT(*) AS n FROM documents d
              JOIN sources s ON s.id = d.source_id
             GROUP BY s.department
            """
        ).fetchall()
    }
    records_approved_by_department = {
        r["department"]: int(r["n"]) for r in conn.execute(
            """
            SELECT s.department AS department, COUNT(*) AS n FROM go_records r
              JOIN sources s ON s.id = r.source_id
             WHERE r.status = 'approved'
             GROUP BY s.department
            """
        ).fetchall()
    }

    result = []
    for department in registry.list_departments(conn):
        source_row = source_rows.get(department)
        documents_found = documents_found_by_department.get(department, 0)
        documents_downloaded = documents_downloaded_by_department.get(department, 0)
        records_approved = records_approved_by_department.get(department, 0)
        records_parsed = health_by_department.get(department, {}).get("total_gos", 0)
        readiness_status = readiness_by_department.get(department, STATUS_NEEDS_ATTENTION)

        request_rows = requests_by_department.get(department, [])
        failed_requests = [r for r in request_rows if r["status"] == "FAILED"]
        durations = [
            (datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds()
            for r in request_rows
            if r["status"] == "COMPLETED" and r["started_at"] and r["finished_at"]
        ]

        level = _certification_level(
            documents_downloaded=documents_downloaded, records_parsed=records_parsed,
            records_approved=records_approved, readiness_status=readiness_status,
        )
        # Publication Yield = Published Records / Downloaded Documents --
        # distinct from success_rate_pct above (parsed/downloaded): yield
        # measures how much of what was downloaded actually made it all the
        # way to citizen-visible, not just how much parsed cleanly.
        publication_yield_pct = min(_pct(records_approved, documents_downloaded), 100.0)

        result.append({
            "department": department,
            "source_url": source_row["url"] if source_row else None,
            "adapter": source_row["adapter"] if source_row else None,
            "documents_found": documents_found,
            "documents_downloaded": documents_downloaded,
            "records_parsed": records_parsed,
            "records_approved": records_approved,
            "records_published": records_approved,
            # Capped at 100: reprocess_record() can create more than one
            # go_record from the same downloaded document (a fresh pending
            # record per re-parse attempt), so the raw ratio can otherwise
            # exceed 100% -- a rate should never read as more than complete.
            "success_rate_pct": min(_pct(records_parsed, documents_downloaded), 100.0),
            "failure_rate_pct": _pct(len(failed_requests), len(request_rows)),
            "publication_yield_pct": publication_yield_pct,
            "yield_status": _yield_status(publication_yield_pct),
            "avg_processing_time_seconds": round(sum(durations) / len(durations), 1) if durations else None,
            "last_successful_run": health_by_department.get(department, {}).get("last_extraction_date"),
            "certification_level": level,
            "certification_label": CERT_LEVEL_LABELS[level],
        })
    return result


def campaign_summary(certification_rows: list[dict]) -> dict:
    """Initiative 6 -- pure aggregation over department_certification()'s own
    rows, passed in rather than recomputed (same pattern as
    department_coverage_kpis()). `total_published_gos` is intentionally NOT
    included here -- callers that also have repository_analytics() (route
    level, to avoid this module's documented circular import with
    analytics.py) should add it alongside this dict.

    Phase 3.9 Initiative 5 (Extraction Completion Monitoring) adds
    departments_extracted/publishable/production_ready -- named counts over
    the SAME certification ladder Phase 3.8 already built (level >= 2/4/5),
    not a second ladder."""
    total = len(certification_rows)
    certified = sum(1 for r in certification_rows if r["certification_level"] == 5)
    requiring_attention = sum(1 for r in certification_rows if r["certification_level"] == 1)
    in_progress = total - certified - requiring_attention
    overall_success_rate = (
        round(sum(r["success_rate_pct"] for r in certification_rows) / total, 1) if total else 0.0
    )
    return {
        "total_departments": total,
        "certified_departments": certified,
        "departments_in_progress": in_progress,
        "departments_requiring_attention": requiring_attention,
        "overall_success_rate_pct": overall_success_rate,
        "departments_extracted": sum(1 for r in certification_rows if r["certification_level"] >= 2),
        "departments_publishable": sum(1 for r in certification_rows if r["certification_level"] >= 4),
        "departments_production_ready": certified,
    }


# ---------------------------------------------------------------------------
# Phase 3.8 Initiative 4 -- Historical Coverage Analysis
#
# Same query shape as analytics._department_year_matrix(), but unfiltered by
# status: this measures how far extraction has actually reached historically
# for a department, not how much has been published.
# ---------------------------------------------------------------------------
def historical_coverage(conn: sqlite3.Connection) -> list[dict]:
    from .. import registry

    rows = conn.execute(
        """
        SELECT s.department AS department, r.go_year AS year, COUNT(*) AS n
          FROM go_records r
          JOIN sources s ON s.id = r.source_id
         WHERE r.go_year IS NOT NULL
         GROUP BY s.department, r.go_year
        """
    ).fetchall()

    by_department: dict[str, list[dict]] = {}
    for row in rows:
        by_department.setdefault(row["department"], []).append(
            {"year": int(row["year"]), "count": int(row["n"])}
        )

    result = []
    for department in registry.list_departments(conn):
        trend = sorted(by_department.get(department, []), key=lambda t: t["year"])
        if not trend:
            result.append({
                "department": department, "earliest_year": None, "latest_year": None,
                "years_covered": 0, "missing_years": [], "coverage_trend": [],
            })
            continue
        years_present = {t["year"] for t in trend}
        earliest, latest = trend[0]["year"], trend[-1]["year"]
        missing = [y for y in range(earliest, latest + 1) if y not in years_present]
        result.append({
            "department": department, "earliest_year": earliest, "latest_year": latest,
            "years_covered": len(years_present), "missing_years": missing, "coverage_trend": trend,
        })
    return result


# ---------------------------------------------------------------------------
# Phase 3.9 Initiative 3 -- Extraction Funnel Analytics
#
# Downloaded -> Parsed -> Review -> Approved -> Published, plus Rejected/
# Duplicate/OCR Failed/Parse Failed. Every number already exists somewhere
# else (go_records.status counts, operations/failures.py) -- this just puts
# them in one funnel-shaped dict, optionally scoped to one real department.
# ---------------------------------------------------------------------------
def extraction_funnel(conn: sqlite3.Connection, *, department: str | None = None) -> dict:
    from . import failures as ops_failures

    dept_clause = " AND s.department = ?" if department else ""
    dept_params = (department,) if department else ()

    documents_downloaded = conn.execute(
        f"SELECT COUNT(*) AS n FROM documents d JOIN sources s ON s.id = d.source_id WHERE 1=1{dept_clause}",
        dept_params,
    ).fetchone()["n"]

    status_rows = conn.execute(
        f"""
        SELECT r.status AS status, COUNT(*) AS n FROM go_records r
          JOIN sources s ON s.id = r.source_id
         WHERE 1=1{dept_clause}
         GROUP BY r.status
        """,
        dept_params,
    ).fetchall()
    by_status = {row["status"]: int(row["n"]) for row in status_rows}
    records_parsed = sum(by_status.values())

    # Real-department variant of dedup.py's duplicate detection (which keys
    # by categorize.py's coarse content bucket, not a real department name)
    # -- same fix already applied to the Review Center's department filter,
    # applied consistently here rather than mixing two vocabularies.
    duplicate_rows = conn.execute(
        f"""
        SELECT r.document_id AS document_id, COUNT(*) AS n FROM go_records r
          JOIN sources s ON s.id = r.source_id
         WHERE r.status = 'pending'{dept_clause}
         GROUP BY r.document_id, s.department
        HAVING COUNT(*) > 1
        """,
        dept_params,
    ).fetchall()
    duplicate_count = sum(int(row["n"]) - 1 for row in duplicate_rows)

    dept_failures = ops_failures.pipeline_failures(conn, department=department, limit=100_000)
    ocr_failed = sum(1 for f in dept_failures if f["stage"] == ops_failures.STAGE_OCR)
    parse_failed = sum(1 for f in dept_failures if f["stage"] == ops_failures.STAGE_PARSING)

    return {
        "documents_downloaded": documents_downloaded,
        "records_parsed": records_parsed,
        "pending_review": by_status.get("pending", 0),
        "approved": by_status.get("approved", 0),
        "published": by_status.get("approved", 0),
        "rejected": by_status.get("rejected", 0),
        "duplicate": duplicate_count,
        "ocr_failed": ocr_failed,
        "parse_failed": parse_failed,
    }
