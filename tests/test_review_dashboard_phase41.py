"""Phase 4.1 -- Review Operations Dashboard + Backlog Campaign Manager."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from goengine.operations import backlog_campaigns as ops_campaigns
from goengine.operations import review_dashboard as ops_review_dashboard
from goengine.workbench.app import create_app
from tests.conftest import login_as


@pytest.fixture
def client(conn, settings, parsed_documents):
    test_client = TestClient(create_app(settings))
    login_as(test_client, conn)
    return test_client


# ---------------------------------------------------------------------------
# review_dashboard.py (data layer)
# ---------------------------------------------------------------------------
def test_throughput_score_excellent_when_backlog_is_empty(conn, parsed_documents):
    from goengine import review

    for row in conn.execute("SELECT id FROM go_records"):
        review.approve(conn, int(row["id"]), reviewer="admin")

    score = ops_review_dashboard.review_throughput_score(conn)
    assert score["pending_review"] == 0
    assert score["label"] == "Excellent"


def test_throughput_score_needs_attention_with_large_untouched_backlog(conn, parsed_documents):
    score = ops_review_dashboard.review_throughput_score(conn)
    assert score["approved_today"] == 0
    assert score["label"] == "Needs Attention"


def test_approved_today_counts_only_todays_approvals(conn, parsed_documents):
    from goengine import review

    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    review.approve(conn, record_id, reviewer="admin")
    assert ops_review_dashboard.approved_today(conn) == 1

    # audit_log is append-only (see schema.sql's trigger) -- simulate an
    # old approval by inserting a fresh row dated in the past, rather than
    # mutating the one just written.
    conn.execute(
        """
        INSERT INTO audit_log (ts, actor, action, entity_type, entity_id)
        VALUES ('2000-01-01T00:00:00+00:00', 'admin', 'record.approved', 'go_record', ?)
        """,
        (record_id,),
    )
    assert ops_review_dashboard.approved_today(conn) == 1


def test_dashboard_summary_has_all_expected_keys(conn, parsed_documents):
    summary = ops_review_dashboard.dashboard_summary(conn)
    for key in (
        "segment_counts", "approved_today", "rejected_today", "throughput",
        "top_blockers", "department_triage", "daily_activity",
        "average_review_time_hours", "department_leaderboard",
    ):
        assert key in summary


def test_average_review_time_is_none_with_no_reviewed_records(conn, parsed_documents):
    assert ops_review_dashboard.average_review_time_hours(conn) is None


# ---------------------------------------------------------------------------
# backlog_campaigns.py (data layer)
# ---------------------------------------------------------------------------
def test_no_active_campaign_returns_none_progress(conn, parsed_documents):
    assert ops_campaigns.active_campaign(conn) is None
    assert ops_campaigns.campaign_progress(conn) is None


def test_start_campaign_captures_starting_count(conn, parsed_documents):
    ops_campaigns.start_campaign(conn, target_count=0, started_by="admin")
    progress = ops_campaigns.campaign_progress(conn)
    assert progress["starting_count"] == 3
    assert progress["current_count"] == 3
    assert progress["reduction_pct"] == 0.0
    # No review activity yet in the last 7 days -> can't estimate.
    assert progress["days_remaining"] is None


def test_cannot_start_a_second_campaign_while_one_is_active(conn, parsed_documents):
    ops_campaigns.start_campaign(conn, target_count=0, started_by="admin")
    with pytest.raises(ops_campaigns.CampaignError):
        ops_campaigns.start_campaign(conn, target_count=0, started_by="admin")


def test_ending_a_campaign_allows_starting_a_new_one(conn, parsed_documents):
    campaign_id = ops_campaigns.start_campaign(conn, target_count=0, started_by="admin")
    ops_campaigns.end_campaign(conn, campaign_id)
    assert ops_campaigns.active_campaign(conn) is None
    # Should not raise.
    ops_campaigns.start_campaign(conn, target_count=1, started_by="admin")


def test_campaign_progress_reflects_real_reduction(conn, parsed_documents):
    from goengine import review

    ops_campaigns.start_campaign(conn, target_count=0, started_by="admin")
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    review.approve(conn, record_id, reviewer="admin")

    progress = ops_campaigns.campaign_progress(conn)
    assert progress["current_count"] == 2
    # reduced 1 of 3 toward a target of 0 -> 1/3 = 33.3%
    assert round(progress["reduction_pct"], 1) == pytest.approx(33.3, abs=0.5)


def test_start_campaign_rejects_negative_target(conn, parsed_documents):
    with pytest.raises(ops_campaigns.CampaignError):
        ops_campaigns.start_campaign(conn, target_count=-1, started_by="admin")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def test_dashboard_page_renders(client):
    response = client.get("/ops/review/dashboard")
    assert response.status_code == 200
    assert "Review Operations Dashboard" in response.text
    assert "Backlog Reduction Campaign" in response.text


def test_dashboard_start_and_end_campaign_via_http(client, conn):
    start = client.post(
        "/ops/review/dashboard/campaign/start", data={"target_count": 0}, follow_redirects=False,
    )
    assert start.status_code == 303
    assert ops_campaigns.active_campaign(conn) is not None

    dashboard = client.get("/ops/review/dashboard")
    assert "Starting Backlog" in dashboard.text

    campaign_id = ops_campaigns.active_campaign(conn)["id"]
    end = client.post(f"/ops/review/dashboard/campaign/{campaign_id}/end", follow_redirects=False)
    assert end.status_code == 303
    assert ops_campaigns.active_campaign(conn) is None


def test_dashboard_rejects_second_campaign_with_flash_error(client, conn):
    client.post("/ops/review/dashboard/campaign/start", data={"target_count": 0})
    response = client.post(
        "/ops/review/dashboard/campaign/start", data={"target_count": 0}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert "campaign_error" in response.headers["location"]
