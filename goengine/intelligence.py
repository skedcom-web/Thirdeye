"""Citizen-facing aggregate intelligence over the Verified GO Database --
backs '/my-area' and '/districts' (Third Eye 4.0 blueprint: My Area
Dashboard, District Intelligence, Financial Intelligence, Timeline
Intelligence -- Phase 1 + Timeline scope only).

Same trust boundary as public.py: every query here is scoped to
go_records.status == 'approved' (see _approved_where) -- a pending or
rejected record must never contribute to a count, a sum, or a ranking.
Every function is built only from the 7 fields the extraction pipeline
actually produces (go_number, go_date, department, subject, budget,
district, scheme_name) -- no fabricated project/status/location data, no
generated summaries. Where there is nothing approved in scope, functions
return None/[] rather than a dict of zeros -- callers render the
empty-state standard ("No approved government actions available for this
district yet.") instead of a zero-filled chart.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .public import DEPARTMENT_LABELS
from .review import STATUS_APPROVED

# Transparent, auditable keyword heuristic against the real `subject` field
# -- NOT an authoritative classification. Mirrors the category taxonomy the
# landing page already uses (cat-roads/cat-health/cat-edu/cat-water/
# cat-agri/cat-community in theme.css) so the whole site stays visually and
# conceptually consistent instead of inventing a different split.
_KEYWORDS = {
    "roads": ("road", "highway", "bridge", "culvert", "flyover"),
    "health": ("hospital", "health", "medical", "clinic", "phc", "dispensary"),
    "edu": ("school", "college", "education", "teacher", "hostel"),
    "water": ("water", "drainage", "sanitation", "sewerage", "pipeline", "irrigation"),
    "agri": ("agriculture", "farmer", "crop", "fisheries"),
    "community": ("housing", "house", "park", "community hall", "public building", "welfare"),
}

# Keys deliberately match the landing page's existing `cat-*` CSS classes
# (theme.css) exactly -- e.g. "edu" not "education" -- so a category card
# here (`class="cat-{{ c.key }}"`) reuses that established styling/coloring
# instead of inventing a parallel taxonomy.
SUBJECT_KEYWORDS: dict[str, tuple[str, ...]] = _KEYWORDS

CATEGORY_LABELS: dict[str, str] = {
    "roads": "Roads",
    "health": "Healthcare",
    "edu": "Education",
    "water": "Water & Sanitation",
    "agri": "Agriculture",
    "community": "Community Development",
}

CATEGORY_ICONS: dict[str, str] = {
    "roads": "🚧",
    "health": "🏥",
    "edu": "🏫",
    "water": "💧",
    "agri": "🌾",
    "community": "🏘",
}

EMPTY_STATE_MESSAGE = "No approved government actions available for this district yet."


def _approved_where(district: str | None) -> tuple[str, list]:
    """Mirrors public.py::_filters -- the one WHERE clause every aggregate
    in this module shares, so a pending/rejected record or an
    out-of-scope district can never leak into a count or a sum."""
    where = ["r.status = ?"]
    params: list = [STATUS_APPROVED]
    if district:
        where.append(
            "EXISTS (SELECT 1 FROM go_fields f WHERE f.record_id = r.id "
            "AND f.field_name = 'district' AND f.superseded_by IS NULL AND f.normalized_value = ?)"
        )
        params.append(district)
    return " AND ".join(where), params


def _parse_date(value: str | None) -> date | None:
    """Defensive ISO-date parse -- skip, never guess, on anything that
    doesn't cleanly parse as YYYY-MM-DD (some OCR'd go_date values are
    imperfect free text even after normalization)."""
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def format_inr(amount: float) -> str:
    """Indian Lakh/Crore formatting matching the blueprint's own example
    ("₹5.6 Crore") -- citizens read large government figures in
    lakhs/crores, not raw rupee counts."""
    if amount >= 1_00_00_000:
        value = f"{amount / 1_00_00_000:.2f}".rstrip("0").rstrip(".")
        return f"₹{value} Crore"
    if amount >= 1_00_000:
        value = f"{amount / 1_00_000:.2f}".rstrip("0").rstrip(".")
        return f"₹{value} Lakh"
    return f"₹{amount:,.0f}"


def area_summary(conn: sqlite3.Connection, district: str | None = None) -> dict | None:
    """Development Summary cards for /my-area. Returns None (not a dict of
    zeros) when nothing approved exists in scope -- the route renders the
    empty-state standard in that case instead of five zero-filled cards."""
    where, params = _approved_where(district)

    total_records = conn.execute(f"SELECT COUNT(*) AS n FROM go_records r WHERE {where}", params).fetchone()["n"]
    if total_records == 0:
        return None

    total_funds = conn.execute(
        f"""
        SELECT COALESCE(SUM(CAST(f.normalized_value AS REAL)), 0) AS total
          FROM go_records r
          JOIN go_fields f ON f.record_id = r.id AND f.field_name = 'budget' AND f.superseded_by IS NULL
         WHERE {where}
        """,
        params,
    ).fetchone()["total"]

    departments_involved = conn.execute(
        f"""
        SELECT COUNT(DISTINCT dc.department_bucket) AS n
          FROM go_records r
          JOIN documents d ON d.id = r.document_id
          JOIN document_categories dc ON dc.document_id = d.id
         WHERE {where} AND dc.department_bucket IS NOT NULL
        """,
        params,
    ).fetchone()["n"]

    schemes_available = conn.execute(
        f"""
        SELECT COUNT(DISTINCT f.normalized_value) AS n
          FROM go_records r
          JOIN go_fields f ON f.record_id = r.id AND f.field_name = 'scheme_name' AND f.superseded_by IS NULL
         WHERE {where}
        """,
        params,
    ).fetchone()["n"]

    new_gos_this_month = conn.execute(
        f"""
        SELECT COUNT(*) AS n
          FROM go_records r
         WHERE {where} AND strftime('%Y-%m', r.reviewed_at) = strftime('%Y-%m', 'now')
        """,
        params,
    ).fetchone()["n"]

    go_dates = conn.execute(
        f"""
        SELECT f.normalized_value AS value
          FROM go_records r
          JOIN go_fields f ON f.record_id = r.id AND f.field_name = 'go_date' AND f.superseded_by IS NULL
         WHERE {where}
        """,
        params,
    ).fetchall()
    parsed_dates = [d for d in (_parse_date(row["value"]) for row in go_dates) if d is not None]
    latest_action_date = max(parsed_dates) if parsed_dates else None

    return {
        "total_funds": total_funds,
        "government_initiatives": total_records,
        "departments_involved": departments_involved,
        "schemes_available": schemes_available,
        "new_gos_this_month": new_gos_this_month,
        "latest_action_date": latest_action_date,
    }


def citizen_category_counts(conn: sqlite3.Connection, district: str | None = None) -> list[dict]:
    """Count of approved GOs per citizen-facing category (see
    SUBJECT_KEYWORDS) -- a transparent, auditable keyword match against the
    real `subject` field, not an authoritative classification. Categories
    with zero matches are omitted rather than shown as an empty stat."""
    where, params = _approved_where(district)
    results = []
    for key, keywords in SUBJECT_KEYWORDS.items():
        like_clauses = " OR ".join("f.normalized_value LIKE ?" for _ in keywords)
        like_params = [f"%{kw}%" for kw in keywords]
        count = conn.execute(
            f"""
            SELECT COUNT(*) AS n
              FROM go_records r
              JOIN go_fields f ON f.record_id = r.id AND f.field_name = 'subject' AND f.superseded_by IS NULL
             WHERE {where} AND ({like_clauses})
            """,
            [*params, *like_params],
        ).fetchone()["n"]
        if count:
            results.append(
                {
                    "key": key,
                    "label": CATEGORY_LABELS[key],
                    "icon": CATEGORY_ICONS[key],
                    "count": count,
                    # First keyword of the set, used to build a representative
                    # "see the actual orders" link (/orders?q=...) -- not a
                    # complete re-expression of the OR'd match, but every
                    # result it shows is real and auditable.
                    "key_example": keywords[0],
                }
            )
    return results


def financial_breakdown(conn: sqlite3.Connection, district: str | None = None) -> dict | None:
    """Budget breakdown by department bucket + a monthly spend trend (last
    6 months with any approved spend), both from the real `budget` field.
    Returns None if there's no budget data in scope at all (empty-state
    standard) -- never renders a zero-filled chart."""
    where, params = _approved_where(district)

    by_department_rows = conn.execute(
        f"""
        SELECT dc.department_bucket AS bucket,
               COALESCE(SUM(CAST(f.normalized_value AS REAL)), 0) AS total,
               COUNT(*) AS n
          FROM go_records r
          JOIN documents d ON d.id = r.document_id
          JOIN document_categories dc ON dc.document_id = d.id
          JOIN go_fields f ON f.record_id = r.id AND f.field_name = 'budget' AND f.superseded_by IS NULL
         WHERE {where} AND dc.department_bucket IS NOT NULL
         GROUP BY dc.department_bucket
        HAVING total > 0
         ORDER BY total DESC
        """,
        params,
    ).fetchall()
    by_department = [
        {
            "bucket": r["bucket"],
            "label": DEPARTMENT_LABELS.get(r["bucket"], r["bucket"].title()),
            "total": r["total"],
            "count": r["n"],
        }
        for r in by_department_rows
    ]

    trend_rows = conn.execute(
        f"""
        SELECT strftime('%Y-%m', godate.normalized_value) AS month,
               COALESCE(SUM(CAST(budget.normalized_value AS REAL)), 0) AS total
          FROM go_records r
          JOIN go_fields godate ON godate.record_id = r.id AND godate.field_name = 'go_date' AND godate.superseded_by IS NULL
          JOIN go_fields budget ON budget.record_id = r.id AND budget.field_name = 'budget' AND budget.superseded_by IS NULL
         WHERE {where}
         GROUP BY month
        HAVING month IS NOT NULL
         ORDER BY month DESC
         LIMIT 6
        """,
        params,
    ).fetchall()
    monthly_trend = [{"month": r["month"], "total": r["total"]} for r in reversed(trend_rows)]

    if not by_department and not monthly_trend:
        return None
    return {"by_department": by_department, "monthly_trend": monthly_trend}


def timeline_feed(
    conn: sqlite3.Connection, district: str | None = None, limit: int = 50, offset: int = 0,
) -> list[dict]:
    """Reverse-chronological feed of approved GOs for /my-area's Timeline
    Intelligence section. Each entry carries exactly the fields approved in
    product review: department, the real subject (template truncates for
    display), budget if present, and the GO's own date -- no synthesized
    summary, no inferred outcome, no fabricated status. Rows without a
    parseable go_date are skipped rather than mis-ordered or guessed at."""
    where, params = _approved_where(district)
    rows = conn.execute(
        f"""
        SELECT r.id AS record_id, dep.normalized_value AS department,
               dc.department_bucket AS department_bucket,
               subj.normalized_value AS subject,
               budget.normalized_value AS budget,
               godate.normalized_value AS go_date
          FROM go_records r
          JOIN documents d ON d.id = r.document_id
          LEFT JOIN document_categories dc ON dc.document_id = d.id
          LEFT JOIN go_fields dep ON dep.record_id = r.id AND dep.field_name = 'department' AND dep.superseded_by IS NULL
          LEFT JOIN go_fields subj ON subj.record_id = r.id AND subj.field_name = 'subject' AND subj.superseded_by IS NULL
          LEFT JOIN go_fields budget ON budget.record_id = r.id AND budget.field_name = 'budget' AND budget.superseded_by IS NULL
          LEFT JOIN go_fields godate ON godate.record_id = r.id AND godate.field_name = 'go_date' AND godate.superseded_by IS NULL
         WHERE {where}
        """,
        params,
    ).fetchall()

    entries = []
    for row in rows:
        parsed = _parse_date(row["go_date"])
        if parsed is None:
            continue
        entries.append(
            {
                "record_id": row["record_id"],
                "department": row["department"],
                "department_label": DEPARTMENT_LABELS.get(row["department_bucket"], "Other"),
                "subject": row["subject"],
                "budget": float(row["budget"]) if row["budget"] else None,
                "go_date": parsed,
            }
        )
    entries.sort(key=lambda e: e["go_date"], reverse=True)
    return entries[offset : offset + limit]


def district_ranking(conn: sqlite3.Connection, *, sort: str = "funds") -> list[dict]:
    """Districts with at least one approved GO, ranked by total funds or by
    count -- mirrors public.py::filter_options()'s 'only show what has
    data' precedent rather than padding out every official district with
    fake zeros for ones with no coverage yet."""
    order_column = "total_funds" if sort == "funds" else "project_count"
    rows = conn.execute(
        f"""
        SELECT f.normalized_value AS district,
               COUNT(*) AS project_count,
               COALESCE(SUM(CAST(budget.normalized_value AS REAL)), 0) AS total_funds
          FROM go_records r
          JOIN go_fields f ON f.record_id = r.id AND f.field_name = 'district' AND f.superseded_by IS NULL
          LEFT JOIN go_fields budget ON budget.record_id = r.id AND budget.field_name = 'budget' AND budget.superseded_by IS NULL
         WHERE r.status = ?
         GROUP BY f.normalized_value
         ORDER BY {order_column} DESC
        """,
        (STATUS_APPROVED,),
    ).fetchall()
    return [
        {"district": r["district"], "project_count": r["project_count"], "total_funds": r["total_funds"]}
        for r in rows
    ]
