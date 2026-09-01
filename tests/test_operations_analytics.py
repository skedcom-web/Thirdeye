"""Phase 3.6 Initiative B/C -- Repository Analytics Center + homepage stats.

"Published" always means status='approved', off go_records.reviewed_at --
every number here is checked against data whose publication state and
timing is fully controlled, not assumed."""

from __future__ import annotations

import pytest

from goengine import review
from goengine.db import utcnow
from goengine.operations import analytics
from goengine.pipeline import run_all


@pytest.fixture
def records(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    return [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]


def test_repository_analytics_totals_with_nothing_published(conn, settings, records):
    result = analytics.repository_analytics(conn, settings)
    totals = result["totals"]
    assert totals["total_published"] == 0
    assert totals["total_pdfs"] == 0
    assert totals["total_years_covered"] == 0
    assert totals["published_today"] == 0
    assert totals["total_departments"] >= 1


def test_repository_analytics_totals_after_publishing(conn, settings, records):
    review.approve(conn, records[0], reviewer="admin")
    review.approve(conn, records[1], reviewer="admin")

    totals = analytics.repository_analytics(conn, settings)["totals"]
    assert totals["total_published"] == 2
    assert totals["total_pdfs"] == 2
    assert totals["published_today"] == 2
    assert totals["published_this_week"] == 2
    assert totals["published_this_month"] == 2
    assert totals["published_this_year"] == 2


def test_published_since_boundary_excludes_older_records(conn, settings, records):
    review.approve(conn, records[0], reviewer="admin")
    # Backdate the approval to well outside any rolling/calendar window.
    conn.execute(
        "UPDATE go_records SET reviewed_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (records[0],)
    )
    totals = analytics.repository_analytics(conn, settings)["totals"]
    assert totals["total_published"] == 1  # still published overall
    assert totals["published_today"] == 0
    assert totals["published_this_week"] == 0
    assert totals["published_this_month"] == 0
    assert totals["published_this_year"] == 0


def test_department_analytics_reports_go_count_and_growth_trend(conn, settings, records):
    review.approve(conn, records[0], reviewer="admin")
    review.approve(conn, records[1], reviewer="admin")

    by_department = {row["department"]: row for row in analytics.department_analytics(conn, settings)}
    # source_id's department is "All Departments" (see conftest.py).
    row = by_department["All Departments"]
    assert row["go_count"] == 2
    assert row["latest_publication_date"] is not None
    assert row["average_quality_score"] is not None
    assert sum(g["count"] for g in row["growth_trend"]) == 2


def test_department_analytics_zero_count_for_unpublished_department(conn, settings, records):
    # Nothing approved yet -- go_count must be 0, not fabricated.
    by_department = {row["department"]: row for row in analytics.department_analytics(conn, settings)}
    row = by_department["All Departments"]
    assert row["go_count"] == 0
    assert row["latest_publication_date"] is None
    assert row["growth_trend"] == []


def test_year_analytics_groups_by_year_and_department_distribution(conn, settings, records):
    for record_id in records:
        review.approve(conn, record_id, reviewer="admin")

    by_year = {row["year"]: row for row in analytics.year_analytics(conn)}
    # sampledata's 3 GOs span 2026-01, 2026-02, 2026-03 -- all the same year.
    assert 2026 in by_year
    assert by_year[2026]["go_count"] == 3
    assert by_year[2026]["department_distribution"] == [{"department": "All Departments", "count": 3}]


def test_homepage_statistics_with_nothing_published(conn, settings, records):
    stats = analytics.homepage_statistics(conn)
    assert stats == {
        "total_published": 0, "departments_covered": 0, "years_covered": 0, "latest_publication_date": None,
    }


def test_homepage_statistics_after_publishing(conn, settings, records):
    review.approve(conn, records[0], reviewer="admin")
    stats = analytics.homepage_statistics(conn)
    assert stats["total_published"] == 1
    assert stats["departments_covered"] == 1
    assert stats["years_covered"] == 1
    assert stats["latest_publication_date"] is not None
