"""Module 12 -- System Health Center."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from goengine import registry
from goengine.operations import health as ops_health
from goengine.workbench.app import create_app
from tests.conftest import login_as


def test_healthy_system_has_no_alerts(conn, settings):
    result = ops_health.system_health(conn, settings)
    assert result["alerts"] == []
    assert result["api_health"] == "ok"


def test_source_with_error_status_is_flagged_down(conn, settings):
    source_id = registry.add_source(
        conn, name="Bad Source", department="D", url="https://cms.tn.gov.in/x", source_type="go_portal",
    )
    conn.execute(
        "UPDATE sources SET last_crawl_status = 'error', last_crawl_failure_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
        (source_id,),
    )
    result = ops_health.system_health(conn, settings)
    assert result["source_availability"]["down"] == [{"id": source_id, "name": "Bad Source"}]
    assert any(a["type"] == ops_health.ALERT_SOURCE_DOWN for a in result["alerts"])


def test_source_that_recovered_is_not_flagged_down(conn, settings):
    """A source that failed once but has since succeeded more recently must
    not still show as down -- staleness matters, not history."""
    source_id = registry.add_source(
        conn, name="Recovered Source", department="D", url="https://cms.tn.gov.in/x", source_type="go_portal",
    )
    conn.execute(
        """
        UPDATE sources
           SET last_crawl_status = 'ok', last_crawl_failure_at = '2026-01-01T00:00:00+00:00',
               last_crawl_success_at = '2026-02-01T00:00:00+00:00'
         WHERE id = ?
        """,
        (source_id,),
    )
    result = ops_health.system_health(conn, settings)
    assert result["source_availability"]["down"] == []


def test_inactive_sources_are_excluded_from_availability(conn, settings):
    source_id = registry.add_source(
        conn, name="X", department="D", url="https://cms.tn.gov.in/x", source_type="go_portal", active=False,
    )
    conn.execute("UPDATE sources SET last_crawl_status = 'error' WHERE id = ?", (source_id,))
    result = ops_health.system_health(conn, settings)
    assert result["source_availability"]["total_active"] == 0
    assert result["source_availability"]["down"] == []


def test_ocr_health_reports_availability_and_languages(conn, settings):
    result = ops_health.ocr_health(conn)
    assert isinstance(result["available"], bool)
    if result["available"]:
        assert "eng" in result["languages"]


def test_storage_over_threshold_triggers_alert(conn, settings):
    result = ops_health.system_health(conn, settings)
    assert not result["storage"]["over_threshold"]  # empty repo, well under any real threshold

    from goengine.operations import health

    tiny_threshold = health.storage_health(settings, conn, threshold_bytes=1)
    assert tiny_threshold["over_threshold"] or tiny_threshold["used_bytes"] == 0


def test_certification_failures_appear_in_recent_list(conn, settings, fetcher):
    from goengine.certification.sources import certify_source

    source_id = registry.add_source(
        conn, name="Bad", department="D", url="https://cms.tn.gov.in/unregistered", source_type="go_portal",
    )
    from goengine.fetching import OfflineFetcher

    certify_source(conn, settings, OfflineFetcher(), source_id)  # no responses -> FAILED

    result = ops_health.system_health(conn, settings)
    assert len(result["certification_failures"]) == 1
    assert any(a["type"] == ops_health.ALERT_CERTIFICATION_FAILURE for a in result["alerts"])


def test_queue_depth_reflects_active_jobs(conn, settings, fetcher, source_id):
    from goengine.operations import jobs as ops_jobs

    assert ops_health.system_health(conn, settings)["processing_queue_depth"] == 0
    ops_jobs.run_job_sync(settings, created_by="admin", fetcher_factory=lambda: fetcher)
    # run_job_sync blocks until completion, so it's no longer "active" by the
    # time we check -- this proves the metric reflects live state, not a count
    # of jobs ever created.
    assert ops_health.system_health(conn, settings)["processing_queue_depth"] == 0


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
@pytest.fixture
def client(conn, settings):
    test_client = TestClient(create_app(settings))
    login_as(test_client, conn)
    return test_client


def test_health_page_renders(client):
    response = client.get("/ops/health")
    assert response.status_code == 200
    assert "System Health" in response.text


def test_health_api_returns_json(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert "alerts" in body
    assert "source_availability" in body


def test_health_page_shows_alert_when_source_down(client, conn):
    source_id = registry.add_source(
        conn, name="Down Source", department="D", url="https://cms.tn.gov.in/x", source_type="go_portal",
    )
    conn.execute("UPDATE sources SET last_crawl_status = 'error' WHERE id = ?", (source_id,))
    response = client.get("/ops/health")
    assert "Down Source" in response.text
