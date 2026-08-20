"""Module 8 -- Publication Control Center."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from goengine import review
from goengine.operations import departments as ops_departments
from goengine.operations import geography
from goengine.operations import publication as ops_publication
from goengine.workbench.app import create_app
from tests.conftest import login_as


@pytest.fixture
def district_id(conn) -> int:
    state_id = geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    return geography.add_district(conn, state_id=state_id, name="Chennai", code="CHE", actor="admin")


# ---------------------------------------------------------------------------
# Governance gates (data layer)
# ---------------------------------------------------------------------------
def test_cannot_publish_uncertified_district(conn, district_id):
    with pytest.raises(ops_publication.PublicationError, match="not certified"):
        ops_publication.publish_district(conn, district_id, actor="admin")


def test_cannot_publish_certified_district_with_no_approved_records(conn, district_id):
    conn.execute("UPDATE districts SET certification_status = 'CERTIFIED' WHERE id = ?", (district_id,))
    with pytest.raises(ops_publication.PublicationError, match="no approved"):
        ops_publication.publish_district(conn, district_id, actor="admin")


def test_publish_district_succeeds_once_both_gates_pass(conn, district_id, parsed_documents):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    review.approve(conn, record_id, reviewer="admin")
    conn.execute("UPDATE districts SET certification_status = 'CERTIFIED' WHERE id = ?", (district_id,))

    ops_publication.publish_district(conn, district_id, actor="admin")
    district = geography.get_district(conn, district_id)
    assert district.publication_status == "PUBLISHED"
    assert district.status == "PUBLISHED"


def test_unpublish_reverts_status_but_not_certification(conn, district_id, parsed_documents):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    review.approve(conn, record_id, reviewer="admin")
    conn.execute("UPDATE districts SET certification_status = 'CERTIFIED' WHERE id = ?", (district_id,))
    ops_publication.publish_district(conn, district_id, actor="admin")

    ops_publication.unpublish_district(conn, district_id, actor="admin", reason="data quality concern")
    district = geography.get_district(conn, district_id)
    assert district.publication_status == "NOT_PUBLISHED"
    assert district.status == "CERTIFIED"  # not reverted to PENDING -- it's still certified
    assert district.certification_status == "CERTIFIED"


def test_unpublish_requires_a_reason(conn, district_id, parsed_documents):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    review.approve(conn, record_id, reviewer="admin")
    conn.execute("UPDATE districts SET certification_status = 'CERTIFIED' WHERE id = ?", (district_id,))
    ops_publication.publish_district(conn, district_id, actor="admin")

    with pytest.raises(ops_publication.PublicationError, match="reason"):
        ops_publication.unpublish_district(conn, district_id, actor="admin", reason="")


def test_publish_unknown_district_raises(conn):
    with pytest.raises(LookupError):
        ops_publication.publish_district(conn, 9999, actor="admin")


def test_department_without_bucket_cannot_be_published(conn):
    dept_id = ops_departments.add_department(conn, name="Fisheries", actor="admin")
    with pytest.raises(ops_publication.PublicationError, match="no acquisition bucket"):
        ops_publication.publish_department(conn, dept_id, actor="admin")


def test_department_needs_a_certified_source_to_publish(conn, parsed_documents):
    dept_id = ops_departments.add_department(conn, name="Health", bucket_key="health", actor="admin")
    with pytest.raises(ops_publication.PublicationError, match="certified source"):
        ops_publication.publish_department(conn, dept_id, actor="admin")


def test_publish_department_succeeds_once_both_gates_pass(conn, parsed_documents):
    dept_id = ops_departments.add_department(conn, name="Health", bucket_key="health", actor="admin")
    conn.execute("UPDATE sources SET certification_status = 'CERTIFIED'")

    record_id = conn.execute(
        """
        SELECT r.id FROM go_records r JOIN documents d ON d.id = r.document_id
          JOIN document_categories c ON c.document_id = d.id
         WHERE c.department_bucket = 'health'
        """
    ).fetchone()["id"]
    review.approve(conn, record_id, reviewer="admin")

    ops_publication.publish_department(conn, dept_id, actor="admin")
    dept = ops_departments.get_department(conn, dept_id)
    assert dept.publication_status == "PUBLISHED"


def test_publication_coverage_counts(conn, district_id, parsed_documents):
    coverage_before = ops_publication.publication_coverage(conn)
    assert coverage_before["districts_total"] == 1
    assert coverage_before["districts_published"] == 0

    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    review.approve(conn, record_id, reviewer="admin")
    conn.execute("UPDATE districts SET certification_status = 'CERTIFIED' WHERE id = ?", (district_id,))
    ops_publication.publish_district(conn, district_id, actor="admin")

    coverage_after = ops_publication.publication_coverage(conn)
    assert coverage_after["districts_published"] == 1


def test_publication_writes_audit_trail(conn, district_id, parsed_documents):
    from goengine import audit

    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    review.approve(conn, record_id, reviewer="admin")
    conn.execute("UPDATE districts SET certification_status = 'CERTIFIED' WHERE id = ?", (district_id,))
    ops_publication.publish_district(conn, district_id, actor="admin")

    actions = [e.action for e in audit.trail(conn, entity_type="district", entity_id=district_id)]
    assert "district.published" in actions


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
@pytest.fixture
def client(conn, settings, parsed_documents):
    test_client = TestClient(create_app(settings))
    login_as(test_client, conn)
    return test_client


def test_publication_hub_renders(client):
    response = client.get("/ops/publication")
    assert response.status_code == 200
    assert "Publish districts" in response.text


def test_publish_district_via_http_blocked_until_certified(client, conn, district_id):
    response = client.post(f"/ops/publication/districts/{district_id}/publish")
    assert response.status_code == 400


def test_publish_district_via_http_succeeds(client, conn, district_id):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    review.approve(conn, record_id, reviewer="admin")
    conn.execute("UPDATE districts SET certification_status = 'CERTIFIED' WHERE id = ?", (district_id,))

    response = client.post(f"/ops/publication/districts/{district_id}/publish", follow_redirects=False)
    assert response.status_code == 303
    assert "PUBLISHED" in client.get("/ops/publication").text


def test_reviewer_cannot_publish(conn, settings, district_id, parsed_documents):
    client = TestClient(create_app(settings))
    login_as(client, conn, username="reviewer1", role="reviewer")
    response = client.post(f"/ops/publication/districts/{district_id}/publish")
    assert response.status_code == 403


def test_certifying_a_source_auto_refreshes_its_district(conn, settings, district_id):
    # Publication Control used to stay stuck on PENDING even after certifying
    # every source in a district, because certifying a source never told the
    # district to recompute its own certification_status -- an admin had to
    # separately remember a "Refresh Certification" button on a different
    # page. Certifying via HTTP should now flip the district's status with
    # no extra step. An unreachable source (empty OfflineFetcher, nothing
    # registered) is enough to prove this -- FAILED still isn't PENDING.
    from fastapi.testclient import TestClient

    from goengine.fetching import OfflineFetcher
    from goengine.operations import sources as ops_sources
    from goengine.workbench.app import create_app, get_fetcher

    district = geography.get_district(conn, district_id)
    src_id = ops_sources.create_source(
        conn, name="Chennai Portal", department="X", url="https://cms.tn.gov.in/chennai",
        source_type="go_portal", state_id=district.state_id, district_id=district_id, actor="admin",
    )
    assert geography.get_district(conn, district_id).certification_status == "PENDING"

    app = create_app(settings)
    app.dependency_overrides[get_fetcher] = lambda: OfflineFetcher()
    client = TestClient(app)
    login_as(client, conn)

    response = client.post(f"/certification/sources/{src_id}/certify", follow_redirects=False)
    assert response.status_code == 303

    assert geography.get_district(conn, district_id).certification_status == "FAILED"


def test_state_admin_cannot_publish_another_states_district(conn, settings, district_id, parsed_documents):
    from goengine.operations import auth as ops_auth

    other_state_id = geography.add_state(conn, name="Kerala", code="KL", actor="admin")
    client = TestClient(create_app(settings))
    login_as(client, conn, username="kl_admin", role=ops_auth.ROLE_STATE_ADMIN, state_id=other_state_id)

    response = client.post(f"/ops/publication/districts/{district_id}/publish")
    assert response.status_code == 403
