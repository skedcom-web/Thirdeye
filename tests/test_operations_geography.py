"""Modules 1-3 -- State, District, Department management."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from goengine.operations import departments as ops_departments
from goengine.operations import geography
from goengine.workbench.app import create_app
from tests.conftest import login_as


# ---------------------------------------------------------------------------
# Module 1: States (data layer)
# ---------------------------------------------------------------------------
def test_add_and_list_state(conn):
    state_id = geography.add_state(conn, name="Tamil Nadu", code="tn", actor="admin")
    states = geography.list_states(conn)
    assert states[0].id == state_id
    assert states[0].code == "TN"  # normalized uppercase
    assert states[0].status == "NEW"


def test_duplicate_state_name_or_code_rejected(conn):
    geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    with pytest.raises(geography.GeographyError):
        geography.add_state(conn, name="Tamil Nadu", code="XX", actor="admin")
    with pytest.raises(geography.GeographyError):
        geography.add_state(conn, name="Other", code="TN", actor="admin")


def test_state_lifecycle_is_forward_only(conn):
    state_id = geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    geography.set_state_status(conn, state_id, "CONFIGURED", actor="admin")
    geography.set_state_status(conn, state_id, "ACTIVE", actor="admin")
    with pytest.raises(geography.GeographyError, match="backward"):
        geography.set_state_status(conn, state_id, "NEW", actor="admin")


def test_state_can_be_retired_from_any_stage(conn):
    state_id = geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    geography.set_state_status(conn, state_id, "RETIRED", actor="admin")
    assert geography.get_state(conn, state_id).status == "RETIRED"


def test_state_active_flag_is_independent_of_lifecycle(conn):
    state_id = geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    geography.set_state_active(conn, state_id, False, actor="admin")
    state = geography.get_state(conn, state_id)
    assert state.status == "NEW"
    assert state.active is False


# ---------------------------------------------------------------------------
# Module 2: Districts (data layer)
# ---------------------------------------------------------------------------
@pytest.fixture
def state_id(conn) -> int:
    return geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")


def test_add_district_requires_a_real_state(conn):
    with pytest.raises(LookupError):
        geography.add_district(conn, state_id=9999, name="Chennai", code="CHE", actor="admin")


def test_district_lifecycle_is_forward_only(conn, state_id):
    district_id = geography.add_district(conn, state_id=state_id, name="Chennai", code="CHE", actor="admin")
    geography.set_district_status(conn, district_id, "CONFIGURED", actor="admin")
    with pytest.raises(geography.GeographyError, match="backward"):
        geography.set_district_status(conn, district_id, "NEW", actor="admin")


def test_district_certification_pending_with_no_sources(conn, state_id):
    district_id = geography.add_district(conn, state_id=state_id, name="Chennai", code="CHE", actor="admin")
    assert geography.applicable_sources(conn, district_id) == []
    assert geography.refresh_district_certification(conn, district_id, actor="admin") == "PENDING"


def test_state_wide_source_counts_toward_every_district(conn, state_id):
    """A source with district_id=NULL is state-wide and implicitly covers
    every district in its state -- the documented modeling decision for
    Phase 3, since Phase 1/2 sources are fundamentally state-level."""
    from goengine import registry

    registry.add_source(
        conn, name="TN GO Portal", department="All", url="https://cms.tn.gov.in/go-search",
        source_type="go_portal",
    )
    conn.execute("UPDATE sources SET state_id = ?, certification_status = 'CERTIFIED' WHERE state_id IS NULL", (state_id,))

    d1 = geography.add_district(conn, state_id=state_id, name="Chennai", code="CHE", actor="admin")
    d2 = geography.add_district(conn, state_id=state_id, name="Madurai", code="MDU", actor="admin")

    assert len(geography.applicable_sources(conn, d1)) == 1
    assert len(geography.applicable_sources(conn, d2)) == 1
    assert geography.refresh_district_certification(conn, d1, actor="admin") == "CERTIFIED"
    assert geography.refresh_district_certification(conn, d2, actor="admin") == "CERTIFIED"


def test_district_scoped_source_does_not_leak_to_other_districts(conn, state_id):
    from goengine import registry

    registry.add_source(
        conn, name="Chennai Collectorate", department="Chennai", url="https://cms.tn.gov.in/chennai",
        source_type="department_site",
    )
    d1 = geography.add_district(conn, state_id=state_id, name="Chennai", code="CHE", actor="admin")
    d2 = geography.add_district(conn, state_id=state_id, name="Madurai", code="MDU", actor="admin")
    conn.execute(
        "UPDATE sources SET state_id = ?, district_id = ?, certification_status = 'CERTIFIED' WHERE state_id IS NULL",
        (state_id, d1),
    )
    assert len(geography.applicable_sources(conn, d1)) == 1
    assert len(geography.applicable_sources(conn, d2)) == 0


def test_districts_affected_by_source_is_the_reverse_of_applicable_sources(conn, state_id):
    from goengine import registry

    d1 = geography.add_district(conn, state_id=state_id, name="Chennai", code="CHE", actor="admin")
    d2 = geography.add_district(conn, state_id=state_id, name="Madurai", code="MDU", actor="admin")

    scoped_id = registry.add_source(
        conn, name="Chennai Collectorate", department="Chennai", url="https://cms.tn.gov.in/chennai",
        source_type="department_site",
    )
    conn.execute("UPDATE sources SET state_id = ?, district_id = ? WHERE id = ?", (state_id, d1, scoped_id))
    assert geography.districts_affected_by_source(conn, scoped_id) == [d1]

    statewide_id = registry.add_source(
        conn, name="TN GO Portal", department="All", url="https://cms.tn.gov.in/go-search",
        source_type="go_portal",
    )
    conn.execute("UPDATE sources SET state_id = ? WHERE id = ?", (state_id, statewide_id))
    assert sorted(geography.districts_affected_by_source(conn, statewide_id)) == sorted([d1, d2])

    assert geography.districts_affected_by_source(conn, 999999) == []


def test_district_certification_reflects_mixed_source_status(conn, state_id):
    from goengine import registry

    registry.add_source(conn, name="A", department="X", url="https://cms.tn.gov.in/a", source_type="go_portal")
    registry.add_source(conn, name="B", department="X", url="https://cms.tn.gov.in/b", source_type="go_portal")
    conn.execute("UPDATE sources SET state_id = ? WHERE state_id IS NULL", (state_id,))
    conn.execute("UPDATE sources SET certification_status = 'CERTIFIED' WHERE name = 'A'")
    conn.execute("UPDATE sources SET certification_status = 'FAILED' WHERE name = 'B'")

    district_id = geography.add_district(conn, state_id=state_id, name="Chennai", code="CHE", actor="admin")
    assert geography.refresh_district_certification(conn, district_id, actor="admin") == "PARTIALLY_CERTIFIED"


def test_district_status_advances_to_certified_when_fully_certified(conn, state_id):
    from goengine import registry

    registry.add_source(conn, name="A", department="X", url="https://cms.tn.gov.in/a", source_type="go_portal")
    conn.execute("UPDATE sources SET state_id = ?, certification_status = 'CERTIFIED' WHERE state_id IS NULL", (state_id,))

    district_id = geography.add_district(conn, state_id=state_id, name="Chennai", code="CHE", actor="admin")
    geography.refresh_district_certification(conn, district_id, actor="admin")
    assert geography.get_district(conn, district_id).status == "CERTIFIED"


# ---------------------------------------------------------------------------
# Module 3: Departments (data layer)
# ---------------------------------------------------------------------------
def test_seed_departments_is_idempotent(conn):
    first = ops_departments.seed(conn, actor="admin")
    second = ops_departments.seed(conn, actor="admin")
    assert first == second
    assert len(ops_departments.list_departments(conn)) == len(ops_departments.SEED_DEPARTMENTS)


def test_department_with_bucket_reports_real_metrics(conn, parsed_documents):
    dept_id = ops_departments.add_department(conn, name="Health", bucket_key="health", actor="admin")
    dept = ops_departments.get_department(conn, dept_id)
    metrics = ops_departments.department_metrics(conn, dept)
    assert metrics["tracked"] is True
    assert metrics["document_count"] == 1  # GO-123-2026.pdf, health bucket
    assert metrics["target"] == 50


def test_department_without_bucket_reports_not_tracked(conn):
    dept_id = ops_departments.add_department(conn, name="Fisheries", actor="admin")
    dept = ops_departments.get_department(conn, dept_id)
    metrics = ops_departments.department_metrics(conn, dept)
    assert metrics == {"tracked": False, "document_count": None, "target": None, "accuracy": None}


def test_invalid_bucket_key_rejected(conn):
    with pytest.raises(ops_departments.DepartmentError):
        ops_departments.add_department(conn, name="X", bucket_key="not_a_real_bucket", actor="admin")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
@pytest.fixture
def client(conn, settings):
    test_client = TestClient(create_app(settings))
    login_as(test_client, conn)
    return test_client


def test_ops_hub_renders(client):
    response = client.get("/ops")
    assert response.status_code == 200
    assert "Administration" in response.text


def test_states_page_add_and_list(client, conn):
    client.post("/ops/states/add", data={"name": "Tamil Nadu", "code": "TN"})
    response = client.get("/ops/states")
    assert "Tamil Nadu" in response.text
    assert "TN" in response.text


def test_non_admin_cannot_add_state(conn, settings):
    client = TestClient(create_app(settings))
    login_as(client, conn, username="reviewer1", role="reviewer")
    response = client.post("/ops/states/add", data={"name": "Kerala", "code": "KL"})
    assert response.status_code == 403


def test_state_admin_cannot_add_district_outside_their_state(conn, settings):
    from goengine.operations import auth as ops_auth

    tn_id = geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    kl_id = geography.add_state(conn, name="Kerala", code="KL", actor="admin")

    client = TestClient(create_app(settings))
    login_as(client, conn, username="tn_admin", role=ops_auth.ROLE_STATE_ADMIN, state_id=tn_id)

    ok = client.post(
        "/ops/districts/add", data={"state_id": tn_id, "name": "Chennai", "code": "CHE"},
        follow_redirects=False,
    )
    assert ok.status_code == 303

    blocked = client.post("/ops/districts/add", data={"state_id": kl_id, "name": "Kochi", "code": "KOC"})
    assert blocked.status_code == 403


def test_departments_seed_via_http(client):
    response = client.post("/ops/departments/seed", follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/ops/departments")
    assert "Health" in page.text
    assert "not yet trackable" in page.text
