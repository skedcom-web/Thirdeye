"""Module 7 -- Review Workbench extension: typed queues + escalation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from goengine import registry
from goengine.operations import review as ops_review
from goengine.workbench.app import create_app
from tests.conftest import login_as


# ---------------------------------------------------------------------------
# Escalation (data layer)
# ---------------------------------------------------------------------------
def test_escalate_does_not_change_record_status(conn, parsed_documents):
    from goengine import review

    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    ops_review.escalate(conn, record_id, escalated_by="reviewer1", reason="looks off")

    summary = review.get_summary(conn, record_id)
    assert summary.status == "pending"  # escalation is a flag, not a decision


def test_escalate_requires_reason_and_identity(conn, parsed_documents):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    with pytest.raises(ops_review.OperationsError):
        ops_review.escalate(conn, record_id, escalated_by="", reason="x")
    with pytest.raises(ops_review.OperationsError):
        ops_review.escalate(conn, record_id, escalated_by="x", reason="")


def test_escalate_unknown_record_raises(conn, parsed_documents):
    with pytest.raises(LookupError):
        ops_review.escalate(conn, 9999, escalated_by="x", reason="y")


def test_resolve_escalation_removes_it_from_open_list(conn, parsed_documents):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    escalation_id = ops_review.escalate(conn, record_id, escalated_by="reviewer1", reason="check this")

    assert len(ops_review.open_escalations(conn)) == 1
    ops_review.resolve_escalation(conn, escalation_id, resolved_by="admin", note="fine")
    assert len(ops_review.open_escalations(conn)) == 0


def test_escalations_for_record_lists_history(conn, parsed_documents):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    ops_review.escalate(conn, record_id, escalated_by="a", reason="first")
    ops_review.escalate(conn, record_id, escalated_by="b", reason="second")
    history = ops_review.escalations_for_record(conn, record_id)
    assert len(history) == 2


def test_escalation_audit_trail(conn, parsed_documents):
    from goengine import audit

    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    escalation_id = ops_review.escalate(conn, record_id, escalated_by="reviewer1", reason="check")
    ops_review.resolve_escalation(conn, escalation_id, resolved_by="admin")

    actions = [e.action for e in audit.trail(conn, entity_type="go_record", entity_id=record_id)]
    assert "record.escalated" in actions
    assert "record.escalation_resolved" in actions


# ---------------------------------------------------------------------------
# Typed queues (data layer)
# ---------------------------------------------------------------------------
def test_ocr_queue_only_contains_records_needing_ocr(conn, parsed_documents):
    all_records = conn.execute("SELECT id FROM go_records").fetchall()
    # None of the demo fixtures need OCR (digital text layer).
    assert ops_review.queue_by_type(conn, ops_review.QUEUE_OCR) == []
    assert len(ops_review.queue_by_type(conn, ops_review.QUEUE_EXTRACTION)) == len(all_records)


def test_extraction_queue_paginates(conn, parsed_documents):
    """Regression test: a queue capped at a fixed limit with no offset
    silently hides everything past that limit and there's no way to reach
    it. queue_by_type's offset param is what /ops/review's Previous/Next
    controls are built on."""
    all_records = ops_review.queue_by_type(conn, ops_review.QUEUE_EXTRACTION, limit=1000)
    assert len(all_records) >= 2

    page1 = ops_review.queue_by_type(conn, ops_review.QUEUE_EXTRACTION, limit=2, offset=0)
    page2 = ops_review.queue_by_type(conn, ops_review.QUEUE_EXTRACTION, limit=2, offset=2)
    assert len(page1) == 2
    assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})
    assert {r["id"] for r in page1} | {r["id"] for r in page2} == {r["id"] for r in all_records[:4]}


def test_failure_queue_reflects_recorded_failures(conn, parsed_documents):
    from goengine.certification import golden
    from goengine.certification.benchmark import run_certification_benchmark
    from goengine.certification.failures import record_failures

    golden_id = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    for field_name in golden.SCORED_FIELDS:
        golden.annotate_field(conn, golden_id, field_name, "wrong on purpose", annotator="alex")

    result = run_certification_benchmark(conn)
    record_failures(conn, result.run_id, result.mismatches)

    failure_queue = ops_review.queue_by_type(conn, ops_review.QUEUE_FAILURE)
    assert len(failure_queue) == 1


def test_queue_counts_sums_correctly(conn, parsed_documents):
    counts = ops_review.queue_counts(conn)
    assert counts[ops_review.QUEUE_EXTRACTION] == 3
    assert counts[ops_review.QUEUE_OCR] == 0


def test_department_filter_narrows_to_matching_documents_only(conn, parsed_documents):
    """`department` narrows on the record's real registered department
    (sources.department), not categorize.py's coarse content bucket -- real
    deployments register one source per real department name (e.g. "Health
    and Family Welfare"), so filtering must actually separate them."""
    doc_id = parsed_documents[0]
    other_source_id = registry.add_source(
        conn, name="Health Department Site", department="Health and Family Welfare",
        url="https://cms.tn.gov.in/health", source_type="go_portal",
    )
    conn.execute("UPDATE go_records SET source_id = ? WHERE document_id = ?", (other_source_id, doc_id))

    unfiltered = ops_review.queue_counts(conn)
    assert unfiltered[ops_review.QUEUE_EXTRACTION] == 3

    moved = ops_review.queue_counts(conn, department="Health and Family Welfare")
    assert moved[ops_review.QUEUE_EXTRACTION] == 1

    remaining = ops_review.queue_counts(conn, department="All Departments")
    assert remaining[ops_review.QUEUE_EXTRACTION] == 2

    # A department nothing is registered under must return zero everywhere,
    # not error -- distinct from the "unknown department string" case, which
    # the HTTP route rejects outright (see below).
    assert ops_review.queue_counts(conn, department="Nonexistent Department")[ops_review.QUEUE_EXTRACTION] == 0


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
@pytest.fixture
def client(conn, settings, parsed_documents):
    test_client = TestClient(create_app(settings))
    login_as(test_client, conn)
    return test_client


def test_review_hub_renders_all_queue_types(client):
    for queue in ("extraction", "ocr", "metadata", "failure"):
        response = client.get(f"/ops/review?queue={queue}")
        assert response.status_code == 200, queue


def test_review_hub_rejects_unknown_queue(client):
    response = client.get("/ops/review?queue=not_a_real_queue")
    assert response.status_code == 400


def test_review_hub_department_filter_via_http(client, conn, parsed_documents):
    department = "All Departments"  # the parsed_documents fixture's one registered source

    response = client.get("/ops/review", params={"queue": "extraction", "department": department})
    assert response.status_code == 200
    assert f'value="{department}" selected' in response.text


def test_review_hub_rejects_unknown_department(client):
    response = client.get("/ops/review?queue=extraction&department=not-a-real-department")
    assert response.status_code == 400


def test_escalate_and_resolve_via_http(client, conn):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]

    escalate = client.post(f"/records/{record_id}/escalate", data={"reason": "double check this"}, follow_redirects=False)
    assert escalate.status_code == 303

    hub = client.get("/ops/review")
    assert "double check this" in hub.text

    escalation_id = ops_review.open_escalations(conn)[0]["id"]
    resolve = client.post(f"/ops/review/escalations/{escalation_id}/resolve", data={"note": "checked"}, follow_redirects=False)
    assert resolve.status_code == 303
    assert len(ops_review.open_escalations(conn)) == 0


def test_read_only_user_cannot_escalate(conn, settings, parsed_documents):
    client = TestClient(create_app(settings))
    login_as(client, conn, username="viewer1", role="read_only")

    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    response = client.post(f"/records/{record_id}/escalate", data={"reason": "x"})
    assert response.status_code == 403
