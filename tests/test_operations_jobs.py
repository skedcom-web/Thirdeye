"""Source-scoping for extraction runs, and the local-only Extraction Center.

Cloud extraction (this server crawling tn.gov.in directly) was removed --
Render's network is blocked at the TCP level by TN government hosts, so it
could never succeed in production. The local extraction agent (cli.py's
`agent-daemon`) is the only extraction path now; `/ops/jobs/start` always
queues a request for it via operations/extraction_queue.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from goengine.operations import jobs as ops_jobs
from goengine.workbench.app import create_app
from tests.conftest import login_as


# ---------------------------------------------------------------------------
# Data layer -- sources_in_scope
# ---------------------------------------------------------------------------
def test_sources_in_scope_with_no_filter_returns_all_active_sources(conn, source_id):
    rows = ops_jobs.sources_in_scope(conn, state_id=None, district_id=None, department_filter=None)
    assert source_id in [int(r["id"]) for r in rows]


def test_sources_in_scope_filters_by_exact_department_name(conn, source_id):
    # source_id's department is "All Departments" (see conftest.py's source_id fixture).
    matching = ops_jobs.sources_in_scope(
        conn, state_id=None, district_id=None, department_filter=["All Departments"],
    )
    assert [int(r["id"]) for r in matching] == [source_id]

    non_matching = ops_jobs.sources_in_scope(
        conn, state_id=None, district_id=None, department_filter=["Health and Family Welfare"],
    )
    assert non_matching == []


def test_sources_in_scope_by_state_excludes_other_states(conn, source_id):
    from goengine.operations import geography

    state_id = geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    other_state_id = geography.add_state(conn, name="Kerala", code="KL", actor="admin")
    conn.execute("UPDATE sources SET state_id = ? WHERE id = ?", (other_state_id, source_id))

    rows = ops_jobs.sources_in_scope(conn, state_id=state_id, district_id=None, department_filter=None)
    assert rows == []  # the only source belongs to Kerala, not Tamil Nadu


# ---------------------------------------------------------------------------
# HTTP -- /ops/jobs
# ---------------------------------------------------------------------------
@pytest.fixture
def client(conn, settings, source_id):
    app = create_app(settings)
    test_client = TestClient(app)
    login_as(test_client, conn)
    return test_client


def test_starting_extraction_queues_a_local_request(client, conn):
    response = client.post("/ops/jobs/start", data={}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ops/jobs"

    row = conn.execute("SELECT * FROM extraction_requests ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["status"] == "QUEUED"


def test_jobs_list_renders(client):
    client.post("/ops/jobs/start", data={})
    response = client.get("/ops/jobs")
    assert response.status_code == 200
    assert "Local Agent Status" in response.text
    assert "Job history" not in response.text  # cloud-only panel, removed


def test_reviewer_cannot_start_a_job(conn, settings, source_id):
    app = create_app(settings)
    client = TestClient(app)
    login_as(client, conn, username="reviewer1", role="reviewer")

    response = client.post("/ops/jobs/start", data={})
    assert response.status_code == 403


def test_state_admin_cannot_start_job_for_another_state(conn, settings, source_id):
    from goengine.operations import auth as ops_auth
    from goengine.operations import geography

    tn_id = geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    kl_id = geography.add_state(conn, name="Kerala", code="KL", actor="admin")

    app = create_app(settings)
    client = TestClient(app)
    login_as(client, conn, username="tn_admin", role=ops_auth.ROLE_STATE_ADMIN, state_id=tn_id)

    response = client.post("/ops/jobs/start", data={"state_id": kl_id})
    assert response.status_code == 403


def test_department_multiselect_keeps_the_same_checkbox_contract(client, conn):
    """The searchable multi-select (theme.js's wireDeptMultiselect) is a
    progressive-enhancement layer over the exact same checkboxes -- this
    locks in that jobs_start's form contract (name="departments",
    value=<real department name>) never changed underneath the new UI shell,
    and that it lists every configured department (registry.list_departments),
    not just 4 hardcoded content buckets."""
    from goengine import registry

    registry.add_source(
        conn, name="Health Dept Source", department="Health and Family Welfare",
        url="https://www.tn.gov.in/go.php?dep_id=health", source_type="department_site",
    )
    registry.add_source(
        conn, name="School Education Source", department="School Education",
        url="https://www.tn.gov.in/go.php?dep_id=edu", source_type="department_site",
    )

    page = client.get("/ops/jobs").text
    assert "data-dept-multiselect" in page
    departments = registry.list_departments(conn)
    assert len(departments) >= 3  # the fixture's "All Departments" source plus the two added above
    for department in departments:
        assert f'name="departments" value="{department}"' in page


def test_starting_a_job_with_departments_still_works_via_the_same_form_fields(client, conn):
    response = client.post(
        "/ops/jobs/start",
        data={"departments": ["Health and Family Welfare", "School Education"]},
        follow_redirects=False,
    )
    assert response.status_code == 303

    row = conn.execute("SELECT department_filter FROM extraction_requests ORDER BY id DESC LIMIT 1").fetchone()
    assert "Health and Family Welfare" in row["department_filter"]
