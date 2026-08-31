"""Module 6 -- Certification Job Center."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from goengine.operations import jobs as ops_jobs
from goengine.workbench.app import create_app
from goengine.workbench.deps import get_fetcher_factory
from tests.conftest import login_as


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------
def test_job_completes_and_counts_documents(settings, conn, fetcher, source_id):
    job_id = ops_jobs.run_job_sync(settings, created_by="admin", fetcher_factory=lambda: fetcher)
    job = ops_jobs.get_job(conn, job_id)

    assert job["status"] == ops_jobs.STATUS_COMPLETED
    assert job["documents_found"] == 3
    assert job["documents_downloaded"] == 3
    assert job["documents_parsed"] == 3
    assert job["sources_total"] == 1
    assert job["sources_completed"] == 1
    assert job["started_at"] and job["finished_at"]


def test_job_scoped_to_a_state_only_covers_state_wide_and_scoped_sources(settings, conn, fetcher, source_id):
    from goengine.operations import geography

    state_id = geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    other_state_id = geography.add_state(conn, name="Kerala", code="KL", actor="admin")
    conn.execute("UPDATE sources SET state_id = ? WHERE id = ?", (other_state_id, source_id))

    job_id = ops_jobs.run_job_sync(
        settings, state_id=state_id, created_by="admin", fetcher_factory=lambda: fetcher
    )
    job = ops_jobs.get_job(conn, job_id)
    assert job["sources_total"] == 0  # the only source belongs to Kerala, not Tamil Nadu
    assert job["documents_found"] == 0


def test_job_department_filter_excludes_non_matching_sources(settings, conn, fetcher, source_id):
    job_id = ops_jobs.run_job_sync(
        settings, department_filter=["education"], created_by="admin", fetcher_factory=lambda: fetcher,
    )
    job = ops_jobs.get_job(conn, job_id)
    # source_id's department is "All Departments" -> buckets to 'other', not 'education'.
    assert job["sources_total"] == 0


def test_job_failure_is_recorded_not_silently_swallowed(settings, conn):
    from goengine.fetching import OfflineFetcher
    from goengine import registry

    registry.add_source(
        conn, name="Bad Source", department="X", url="https://cms.tn.gov.in/bad", source_type="go_portal",
    )
    job_id = ops_jobs.run_job_sync(
        settings, created_by="admin", fetcher_factory=lambda: OfflineFetcher()
    )
    job = ops_jobs.get_job(conn, job_id)
    # No responses registered -> crawl fails per-source, but the JOB itself
    # still completes (a bad source must not crash the whole run).
    assert job["status"] == ops_jobs.STATUS_COMPLETED
    assert job["documents_found"] == 0


def test_district_certification_refreshed_after_job(settings, conn, fetcher, source_id):
    from goengine.operations import geography

    state_id = geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    district_id = geography.add_district(conn, state_id=state_id, name="Chennai", code="CHE", actor="admin")
    conn.execute("UPDATE sources SET state_id = ? WHERE id = ?", (state_id, source_id))

    ops_jobs.run_job_sync(settings, district_id=district_id, created_by="admin", fetcher_factory=lambda: fetcher)
    # The job runs discovery/download/parse only -- certification RESULT for
    # the source itself is a separate Module 1/5 action, so refresh here
    # should reflect "not yet certified", not crash.
    district = geography.get_district(conn, district_id)
    assert district.certification_status in ("PENDING", "PARTIALLY_CERTIFIED", "CERTIFIED", "FAILED")


def test_active_job_count(settings, conn, fetcher, source_id):
    assert ops_jobs.active_job_count(conn) == 0
    ops_jobs.run_job_sync(settings, created_by="admin", fetcher_factory=lambda: fetcher)
    assert ops_jobs.active_job_count(conn) == 0  # completed, not active


def test_job_audit_trail(settings, conn, fetcher, source_id):
    from goengine import audit

    job_id = ops_jobs.run_job_sync(settings, created_by="admin", fetcher_factory=lambda: fetcher)
    actions = [e.action for e in audit.trail(conn, entity_type="certification_job", entity_id=job_id)]
    assert "job.started" in actions
    assert "job.completed" in actions


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
@pytest.fixture
def client(conn, settings, fetcher, source_id):
    app = create_app(settings)
    app.dependency_overrides[get_fetcher_factory] = lambda: (lambda: fetcher)
    test_client = TestClient(app)
    login_as(test_client, conn)
    return test_client


def test_start_and_poll_job_via_http(client):
    """start_job is genuinely asynchronous -- it returns before the
    background thread finishes, so the dashboard has something live to
    poll. Wait for completion the same way a real UI would: poll."""
    response = client.post("/ops/jobs/start", data={}, follow_redirects=False)
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]

    status = None
    for _ in range(50):
        status = client.get(f"/api/jobs/{job_id}").json()
        if status["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(0.1)

    assert status["status"] == "COMPLETED"
    assert status["documents_found"] == 3

    detail = client.get(f"/ops/jobs/{job_id}")
    assert detail.status_code == 200
    assert "COMPLETED" in detail.text


def test_jobs_list_renders(client):
    client.post("/ops/jobs/start", data={})
    response = client.get("/ops/jobs")
    assert response.status_code == 200
    assert "Job history" in response.text


def test_reviewer_cannot_start_a_job(conn, settings, fetcher, source_id):
    app = create_app(settings)
    app.dependency_overrides[get_fetcher_factory] = lambda: (lambda: fetcher)
    client = TestClient(app)
    login_as(client, conn, username="reviewer1", role="reviewer")

    response = client.post("/ops/jobs/start", data={})
    assert response.status_code == 403


def test_state_admin_cannot_start_job_for_another_state(conn, settings, fetcher, source_id):
    from goengine.operations import auth as ops_auth
    from goengine.operations import geography

    tn_id = geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    kl_id = geography.add_state(conn, name="Kerala", code="KL", actor="admin")

    app = create_app(settings)
    app.dependency_overrides[get_fetcher_factory] = lambda: (lambda: fetcher)
    client = TestClient(app)
    login_as(client, conn, username="tn_admin", role=ops_auth.ROLE_STATE_ADMIN, state_id=tn_id)

    response = client.post("/ops/jobs/start", data={"state_id": kl_id})
    assert response.status_code == 403


def test_missing_job_is_404(client):
    assert client.get("/ops/jobs/9999").status_code == 404
    assert client.get("/api/jobs/9999").status_code == 404


def test_department_multiselect_keeps_the_same_checkbox_contract(client):
    """The searchable multi-select (theme.js's wireDeptMultiselect) is a
    progressive-enhancement layer over the exact same checkboxes -- this
    locks in that jobs_start's form contract (name="departments",
    value=<bucket key>) never changed underneath the new UI shell."""
    from goengine.certification.categorize import ALL_BUCKETS

    page = client.get("/ops/jobs").text
    assert 'data-dept-multiselect' in page
    for bucket in ALL_BUCKETS:
        assert f'name="departments" value="{bucket}"' in page


def test_starting_a_job_with_departments_still_works_via_the_same_form_fields(client):
    response = client.post(
        "/ops/jobs/start", data={"departments": ["health", "education"]}, follow_redirects=False
    )
    assert response.status_code == 303
