"""Third Eye 4.1.1 -- Citizen Experience Validation Edition.

Unit tests for goengine/operations/engagement.py's write path (log_event)
and report queries (session reconstruction, popularity, purge), plus the
HTTP surface: the visitor cookie, staff-exclusion, category/timeline click
tagging, and the /ops/engagement report itself.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from goengine import review
from goengine.operations import engagement
from goengine.workbench.app import create_app
from tests.conftest import login_as


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _insert(
    conn: sqlite3.Connection, *, visitor_id: str, event_type: str, occurred_at: datetime,
    district: str | None = None, category: str | None = None, record_id: int | None = None,
    query_used: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO engagement_events
            (visitor_id, event_type, path, district, category, record_id, query_used, occurred_at)
        VALUES (?, ?, '/test', ?, ?, ?, ?, ?)
        """,
        (visitor_id, event_type, district, category, record_id, query_used, _iso(occurred_at)),
    )


NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# log_event
# ---------------------------------------------------------------------------
def test_log_event_inserts_a_row(conn):
    engagement.log_event(conn, visitor_id="v1", event_type="my_area_view", path="/my-area", district="Chennai")
    row = conn.execute("SELECT * FROM engagement_events").fetchone()
    assert row["visitor_id"] == "v1"
    assert row["event_type"] == "my_area_view"
    assert row["district"] == "Chennai"
    assert row["query_used"] == 0


def test_log_event_never_raises_on_a_broken_connection(conn):
    conn.close()
    engagement.log_event(conn, visitor_id="v1", event_type="my_area_view", path="/my-area")  # must not raise


# ---------------------------------------------------------------------------
# Session reconstruction
# ---------------------------------------------------------------------------
def test_two_events_over_30_minutes_apart_are_two_sessions(conn):
    _insert(conn, visitor_id="v1", event_type="my_area_view", occurred_at=NOW - timedelta(minutes=50))
    _insert(conn, visitor_id="v1", event_type="my_area_view", occurred_at=NOW)
    metrics = engagement.session_metrics(conn, days=7)
    assert metrics["total_sessions"] == 2


def test_two_events_under_30_minutes_apart_are_one_session(conn):
    _insert(conn, visitor_id="v1", event_type="my_area_view", occurred_at=NOW - timedelta(minutes=10))
    _insert(conn, visitor_id="v1", event_type="orders_view", occurred_at=NOW)
    metrics = engagement.session_metrics(conn, days=7)
    assert metrics["total_sessions"] == 1
    assert metrics["avg_pages_per_session"] == 2


def test_return_visitor_rate_sees_history_outside_the_report_window(conn):
    # Visitor A's only prior visit is 40 days ago -- outside a 30-day
    # report window -- but their session today must still count as a
    # return visit, since return-visitor-rate needs full history, not just
    # the window being reported on.
    _insert(conn, visitor_id="A", event_type="my_area_view", occurred_at=NOW - timedelta(days=40))
    _insert(conn, visitor_id="A", event_type="my_area_view", occurred_at=NOW)
    # Visitor B has never visited before.
    _insert(conn, visitor_id="B", event_type="my_area_view", occurred_at=NOW)

    metrics = engagement.session_metrics(conn, days=30)
    assert metrics["total_sessions"] == 2
    assert metrics["return_visitor_rate"] == pytest.approx(0.5)


def test_dashboard_to_go_conversion(conn):
    _insert(conn, visitor_id="converted", event_type="my_area_view", occurred_at=NOW - timedelta(minutes=5))
    _insert(conn, visitor_id="converted", event_type="go_detail_view", occurred_at=NOW)
    _insert(conn, visitor_id="not-converted", event_type="districts_view", occurred_at=NOW)
    _insert(conn, visitor_id="irrelevant", event_type="go_detail_view", occurred_at=NOW)  # no dashboard visit at all

    metrics = engagement.session_metrics(conn, days=7)
    assert metrics["total_sessions"] == 3
    assert metrics["dashboard_to_go_conversion"] == pytest.approx(0.5)  # 1 of 2 dashboard sessions converted


def test_average_timeline_depth(conn):
    _insert(conn, visitor_id="v1", event_type="my_area_view", occurred_at=NOW - timedelta(minutes=5))
    _insert(conn, visitor_id="v1", event_type="timeline_click", occurred_at=NOW - timedelta(minutes=4))
    _insert(conn, visitor_id="v1", event_type="timeline_click", occurred_at=NOW)
    _insert(conn, visitor_id="v2", event_type="my_area_view", occurred_at=NOW)  # no clicks

    assert engagement.average_timeline_depth(conn, days=7) == pytest.approx(1.0)  # (2 + 0) / 2 sessions


# ---------------------------------------------------------------------------
# Popularity / usage reports
# ---------------------------------------------------------------------------
def test_district_popularity_only_lists_districts_with_views(conn):
    _insert(conn, visitor_id="v1", event_type="my_area_view", occurred_at=NOW, district="Chennai")
    _insert(conn, visitor_id="v2", event_type="my_area_view", occurred_at=NOW, district="Chennai")
    _insert(conn, visitor_id="v3", event_type="my_area_view", occurred_at=NOW, district="Madurai")

    result = engagement.district_popularity(conn, days=7)
    assert result[0] == {"district": "Chennai", "views": 2}
    assert result[1] == {"district": "Madurai", "views": 1}


def test_category_popularity_includes_zero_count_categories(conn):
    _insert(conn, visitor_id="v1", event_type="category_click", occurred_at=NOW, category="roads")
    result = engagement.category_popularity(conn, days=7)
    assert len(result) == len(engagement.intelligence.SUBJECT_KEYWORDS)
    roads = next(c for c in result if c["key"] == "roads")
    assert roads["clicks"] == 1
    zero_count = [c for c in result if c["key"] != "roads"]
    assert all(c["clicks"] == 0 for c in zero_count)


def test_timeline_vs_search_excludes_category_prefilled_searches(conn):
    _insert(conn, visitor_id="v1", event_type="orders_view", occurred_at=NOW, query_used=1)  # a real typed search
    _insert(conn, visitor_id="v2", event_type="orders_view", occurred_at=NOW, query_used=0)  # plain browse or cat= click
    _insert(conn, visitor_id="v3", event_type="timeline_click", occurred_at=NOW)

    result = engagement.timeline_vs_search(conn, days=7)
    assert result == {"timeline_clicks": 1, "searches": 1}


def test_feature_usage_sorted_descending(conn):
    _insert(conn, visitor_id="v1", event_type="my_area_view", occurred_at=NOW)
    _insert(conn, visitor_id="v2", event_type="my_area_view", occurred_at=NOW)
    _insert(conn, visitor_id="v3", event_type="districts_view", occurred_at=NOW)

    result = engagement.feature_usage(conn, days=7)
    assert result[0] == {"event_type": "my_area_view", "count": 2}
    assert result[1] == {"event_type": "districts_view", "count": 1}


def test_total_downloads_from_download_log(conn, parsed_documents):
    from goengine.operations import auth, citizen as ops_citizen

    record_id = int(conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"])
    review.approve(conn, record_id, reviewer="tester")
    user_id = auth.create_user(conn, username="staffer", password="password12345", role=auth.ROLE_PLATFORM_ADMIN)

    ops_citizen.log_download(conn, record_id=record_id, format="pdf", citizen_id=None, staff_user_id=user_id)
    assert engagement.total_downloads(conn, days=7) == 1


# ---------------------------------------------------------------------------
# top_viewed_records
# ---------------------------------------------------------------------------
def test_top_viewed_records_ordering_and_real_metadata(conn, parsed_documents):
    ids = [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]
    for record_id in ids:
        review.approve(conn, record_id, reviewer="tester")

    target, other = ids[0], ids[1]
    _insert(conn, visitor_id="v1", event_type="go_detail_view", occurred_at=NOW, record_id=target)
    _insert(conn, visitor_id="v2", event_type="go_detail_view", occurred_at=NOW, record_id=target)
    _insert(conn, visitor_id="v3", event_type="go_detail_view", occurred_at=NOW, record_id=other)

    top = engagement.top_viewed_records(conn, days=7)
    assert top[0]["record_id"] == target
    assert top[0]["views"] == 2
    assert top[0]["go_number"]  # real extracted metadata, not fabricated


def test_top_viewed_records_drops_a_since_deleted_record(conn, parsed_documents):
    from goengine.operations.dedup import delete_record_and_its_extraction

    ids = [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]
    review.approve(conn, ids[0], reviewer="tester")
    _insert(conn, visitor_id="v1", event_type="go_detail_view", occurred_at=NOW, record_id=ids[0])

    delete_record_and_its_extraction(conn, ids[0])  # FK-safe cascade, same tool the dedup-cleanup UI uses
    assert engagement.top_viewed_records(conn, days=7) == []


# ---------------------------------------------------------------------------
# purge_old_events
# ---------------------------------------------------------------------------
def test_purge_removes_only_rows_past_the_cutoff(conn):
    _insert(conn, visitor_id="old", event_type="my_area_view", occurred_at=NOW - timedelta(days=400))
    _insert(conn, visitor_id="recent", event_type="my_area_view", occurred_at=NOW - timedelta(days=10))

    removed = engagement.purge_old_events(conn, months=12)
    assert removed == 1
    remaining = conn.execute("SELECT visitor_id FROM engagement_events").fetchall()
    assert [r["visitor_id"] for r in remaining] == ["recent"]


def test_purge_never_touches_other_tables(conn, parsed_documents):
    ids = [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]
    review.approve(conn, ids[0], reviewer="tester")
    _insert(conn, visitor_id="old", event_type="my_area_view", occurred_at=NOW - timedelta(days=400))

    before = {
        table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in ("go_records", "audit_log", "citizen_users")
    }
    engagement.purge_old_events(conn, months=12)
    after = {
        table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in ("go_records", "audit_log", "citizen_users")
    }
    assert before == after


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------
@pytest.fixture
def client(conn, settings):
    return TestClient(create_app(settings))


def test_visitor_cookie_set_once_and_reused(client, conn):
    r1 = client.get("/")
    cookie_header = r1.headers.get("set-cookie")
    assert cookie_header is not None and "thirdeye_visitor_id=" in cookie_header

    r2 = client.get("/orders")
    assert r2.headers.get("set-cookie") is None  # not re-set on a returning request

    visitor_ids = {row["visitor_id"] for row in conn.execute("SELECT visitor_id FROM engagement_events").fetchall()}
    assert len(visitor_ids) == 1  # same visitor across both requests


def test_staff_traffic_is_excluded_from_engagement_events(client, conn, parsed_documents):
    ids = [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]
    review.approve(conn, ids[0], reviewer="tester")
    login_as(client, conn)

    client.get("/")
    client.get("/my-area")
    client.get("/districts")
    client.get("/orders")
    client.get(f"/orders/{ids[0]}")

    assert conn.execute("SELECT COUNT(*) AS n FROM engagement_events").fetchone()["n"] == 0


def test_category_click_logs_extra_event_without_counting_as_search(client, conn):
    client.get("/orders?district=Chennai&q=road&cat=roads")
    rows = conn.execute("SELECT event_type, query_used FROM engagement_events ORDER BY id").fetchall()
    event_types = [r["event_type"] for r in rows]
    assert "category_click" in event_types
    orders_view = next(r for r in rows if r["event_type"] == "orders_view")
    assert orders_view["query_used"] == 0  # category click-through, not a typed search


def test_timeline_ref_logs_extra_click_event(client, conn, parsed_documents):
    ids = [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]
    review.approve(conn, ids[0], reviewer="tester")

    client.get(f"/orders/{ids[0]}?ref=timeline")
    event_types = [r["event_type"] for r in conn.execute("SELECT event_type FROM engagement_events").fetchall()]
    assert "go_detail_view" in event_types
    assert "timeline_click" in event_types


def test_ops_engagement_requires_login(client):
    response = client.get("/ops/engagement", follow_redirects=False)
    assert response.status_code in (303, 307)


def test_ops_engagement_renders_with_zero_events(client, conn):
    login_as(client, conn)
    response = client.get("/ops/engagement")
    assert response.status_code == 200
    assert "Engagement Analytics" in response.text
    assert "Privacy" in response.text
    assert "Most Viewed Government Action" not in response.text  # no events yet -- no fabricated KPI


def test_ops_engagement_purge_requires_confirmation(client, conn):
    login_as(client, conn)
    response = client.post("/ops/engagement/purge", data={})
    assert response.status_code == 400


def test_ops_engagement_purge_requires_permission(client, conn):
    login_as(client, conn, role="reviewer")
    response = client.post("/ops/engagement/purge", data={"confirm": "yes"})
    assert response.status_code == 403


def test_ops_engagement_purge_works_end_to_end(client, conn):
    _insert(conn, visitor_id="old", event_type="my_area_view", occurred_at=NOW - timedelta(days=400))
    login_as(client, conn)  # defaults to platform_admin, which has manage_sources
    response = client.post("/ops/engagement/purge", data={"confirm": "yes"}, follow_redirects=False)
    assert response.status_code == 303
    assert conn.execute("SELECT COUNT(*) AS n FROM engagement_events").fetchone()["n"] == 0
