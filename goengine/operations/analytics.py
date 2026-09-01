"""Phase 3.6 Initiative B -- Repository Analytics Center (+ Initiative C's
homepage statistics).

Repository-level publication analytics: how much has been published, by
whom (department), and when (year) -- distinct from operations/quality.py,
which measures whether extraction/data itself is good, not how much of it
has been published. Every number here is computed fresh from
`go_records.reviewed_at` (set exactly once, only by `review.approve()`) --
"published" always means `status='approved'`. Nothing here is fabricated or
cached; a quiet/small repository just shows small real numbers.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .. import registry
from ..review import STATUS_APPROVED
from .quality import department_coverage, department_health


def _utc_boundary(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _published_since(conn: sqlite3.Connection, threshold: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM go_records WHERE status = ? AND reviewed_at >= ?",
        (STATUS_APPROVED, threshold),
    ).fetchone()["n"]


def _department_year_matrix(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One row per (department, year) among approved, published records,
    with a GO count and that bucket's most recent publication -- computed
    once and shared by both department_analytics() and year_analytics()
    below, avoiding two separate passes over the same rows."""
    return conn.execute(
        """
        SELECT s.department AS department, r.go_year AS year,
               COUNT(*) AS n, MAX(r.reviewed_at) AS latest
          FROM go_records r
          JOIN sources s ON s.id = r.source_id
         WHERE r.status = ? AND r.go_year IS NOT NULL
         GROUP BY s.department, r.go_year
        """,
        (STATUS_APPROVED,),
    ).fetchall()


def department_analytics(conn: sqlite3.Connection, settings) -> list[dict]:
    """Per configured department: published GO count, latest publication
    date, average quality score (reused as-is from department_health() --
    spans all extracted records regardless of status, a deliberately
    different denominator than the published-only count here, since the two
    measure different things), and a year-by-year growth trend."""
    matrix = _department_year_matrix(conn)
    by_department: dict[str, dict] = {}
    for row in matrix:
        entry = by_department.setdefault(
            row["department"], {"go_count": 0, "latest_publication_date": None, "growth_trend": []}
        )
        entry["go_count"] += row["n"]
        entry["growth_trend"].append({"year": int(row["year"]), "count": row["n"]})
        if entry["latest_publication_date"] is None or row["latest"] > entry["latest_publication_date"]:
            entry["latest_publication_date"] = row["latest"]

    health_by_department = {h["department"]: h for h in department_health(conn, settings)}

    result = []
    for department in registry.list_departments(conn):
        entry = by_department.get(department, {"go_count": 0, "latest_publication_date": None, "growth_trend": []})
        entry["growth_trend"].sort(key=lambda g: g["year"])
        result.append({
            "department": department,
            "go_count": entry["go_count"],
            "latest_publication_date": entry["latest_publication_date"],
            "average_quality_score": health_by_department.get(department, {}).get("quality_score"),
            "growth_trend": entry["growth_trend"],
        })
    return result


def year_analytics(conn: sqlite3.Connection) -> list[dict]:
    """Per year with ≥1 published GO: count, and which departments
    contributed how many -- reading this across years in publication order
    is Initiative B's "publication trends" view."""
    matrix = _department_year_matrix(conn)
    by_year: dict[int, dict] = {}
    for row in matrix:
        year = int(row["year"])
        entry = by_year.setdefault(year, {"go_count": 0, "department_distribution": []})
        entry["go_count"] += row["n"]
        entry["department_distribution"].append({"department": row["department"], "count": row["n"]})

    result = [{"year": year, **entry} for year, entry in sorted(by_year.items())]
    for entry in result:
        entry["department_distribution"].sort(key=lambda d: -d["count"])
    return result


def repository_analytics(conn: sqlite3.Connection, settings) -> dict:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")

    total_published = conn.execute(
        "SELECT COUNT(*) AS n FROM go_records WHERE status = ?", (STATUS_APPROVED,)
    ).fetchone()["n"]
    total_pdfs = conn.execute(
        "SELECT COUNT(DISTINCT document_id) AS n FROM go_records WHERE status = ?", (STATUS_APPROVED,)
    ).fetchone()["n"]
    total_years_covered = conn.execute(
        "SELECT COUNT(DISTINCT go_year) AS n FROM go_records WHERE status = ? AND go_year IS NOT NULL",
        (STATUS_APPROVED,),
    ).fetchone()["n"]

    return {
        "totals": {
            "total_published": total_published,
            "total_departments": len(registry.list_departments(conn)),
            "total_pdfs": total_pdfs,
            "total_years_covered": total_years_covered,
            "published_today": _published_since(conn, today_start),
            "published_this_week": _published_since(conn, _utc_boundary(7)),  # rolling 7 days
            "published_this_month": _published_since(conn, month_start),  # calendar month
            "published_this_year": _published_since(conn, year_start),  # calendar year
        },
        "by_department": department_analytics(conn, settings),
        "by_year": year_analytics(conn),
    }


def homepage_statistics(conn: sqlite3.Connection) -> dict:
    """Phase 3.6 Initiative C -- the landing page's repository stats row.
    Reuses department_coverage()'s existing "covered" definition rather than
    recomputing it, so the two numbers can never quietly drift apart."""
    total_published = conn.execute(
        "SELECT COUNT(*) AS n FROM go_records WHERE status = ?", (STATUS_APPROVED,)
    ).fetchone()["n"]
    years_covered = conn.execute(
        "SELECT COUNT(DISTINCT go_year) AS n FROM go_records WHERE status = ? AND go_year IS NOT NULL",
        (STATUS_APPROVED,),
    ).fetchone()["n"]
    latest_publication_date = conn.execute(
        "SELECT MAX(reviewed_at) AS ts FROM go_records WHERE status = ?", (STATUS_APPROVED,)
    ).fetchone()["ts"]

    return {
        "total_published": total_published,
        "departments_covered": department_coverage(conn)["covered"],
        "years_covered": years_covered,
        "latest_publication_date": latest_publication_date,
    }
