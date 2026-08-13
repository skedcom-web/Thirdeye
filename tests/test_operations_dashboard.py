"""Module 9 -- Operations Dashboard."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from goengine.operations import dashboard as ops_dashboard
from goengine.operations import geography
from goengine.workbench.app import create_app
from tests.conftest import login_as


def test_summary_with_no_data_does_not_crash(conn, settings):
    summary = ops_dashboard.operations_summary(conn, settings)
    assert summary["active_states"] == 0
    assert summary["total_states"] == 0
    assert summary["accuracy_score"] is None
    assert summary["documents_processed"] == 0


def test_summary_reflects_active_states_and_districts(conn, settings):
    state_id = geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    geography.set_state_status(conn, state_id, "CONFIGURED", actor="admin")
    geography.set_state_status(conn, state_id, "ACTIVE", actor="admin")
    geography.add_district(conn, state_id=state_id, name="Chennai", code="CHE", actor="admin")

    summary = ops_dashboard.operations_summary(conn, settings)
    assert summary["active_states"] == 1
    assert summary["total_states"] == 1
    assert summary["total_districts"] == 1
    assert summary["active_districts"] == 0  # NEW status, not yet CERTIFIED/PUBLISHED


def test_summary_reflects_documents_and_review_queue(conn, settings, parsed_documents):
    summary = ops_dashboard.operations_summary(conn, settings)
    assert summary["documents_processed"] == 3
    assert summary["documents_requiring_review"] == 3


def test_summary_computes_accuracy_score_from_latest_benchmark(conn, settings, parsed_documents):
    from goengine.certification import golden, run_full_certification

    golden_id = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    for field_name in golden.SCORED_FIELDS:
        golden.annotate_field(conn, golden_id, field_name, "value", annotator="alex")

    result = run_full_certification(conn)
    summary = ops_dashboard.operations_summary(conn, settings)
    assert summary["accuracy_score"] is not None
    assert summary["latest_benchmark_run_id"] == result.run_id


def test_summary_reflects_publication_coverage(conn, settings, parsed_documents):
    from goengine import review
    from goengine.operations import publication as ops_publication

    state_id = geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    district_id = geography.add_district(conn, state_id=state_id, name="Chennai", code="CHE", actor="admin")
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    review.approve(conn, record_id, reviewer="admin")
    conn.execute("UPDATE districts SET certification_status = 'CERTIFIED' WHERE id = ?", (district_id,))
    ops_publication.publish_district(conn, district_id, actor="admin")

    summary = ops_dashboard.operations_summary(conn, settings)
    assert summary["publication_coverage"]["districts_published"] == 1


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
@pytest.fixture
def client(conn, settings, parsed_documents):
    test_client = TestClient(create_app(settings))
    login_as(test_client, conn)
    return test_client


def test_dashboard_renders(client):
    response = client.get("/ops/dashboard")
    assert response.status_code == 200
    assert "Operations Dashboard" in response.text
    assert "Certified sources" in response.text


def test_dashboard_shows_prompt_when_no_benchmark_has_run(client):
    response = client.get("/ops/dashboard")
    assert "No certification benchmark has run yet" in response.text
