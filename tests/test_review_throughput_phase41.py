"""Phase 4.1 -- Review Throughput & Backlog Elimination Refinement Patch.

Covers the pieces added on top of the existing Review Center / Failure
Workbench: smart queue segmentation exposed through /ops/review, bulk
reject (Refinement 7), bulk reprocess (Refinement 3), configurable page
size (Refinement 2 of the original blueprint), and Critical Extraction
Failure routing (Refinement 2 of the patch).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from goengine.operations import review as ops_review
from goengine.workbench.app import create_app
from tests.conftest import login_as


@pytest.fixture
def client(conn, settings, parsed_documents):
    test_client = TestClient(create_app(settings))
    login_as(test_client, conn)
    return test_client


def _delete_field(conn, record_id: int, field_name: str) -> None:
    conn.execute(
        "DELETE FROM go_fields WHERE record_id = ? AND field_name = ? AND superseded_by IS NULL",
        (record_id, field_name),
    )


# ---------------------------------------------------------------------------
# Smart queues on the review hub
# ---------------------------------------------------------------------------
def test_review_hub_renders_all_smart_queues(client):
    for queue in ops_review.SMART_QUEUES:
        response = client.get(f"/ops/review?queue={queue}")
        assert response.status_code == 200, queue


def test_ready_for_approval_queue_holds_all_records_by_default(client, conn, parsed_documents):
    response = client.get("/ops/review?queue=ready_for_approval")
    assert response.status_code == 200
    for record_id in conn.execute("SELECT id FROM go_records").fetchall():
        assert f"#{record_id['id']}" in response.text


def test_critical_extraction_failure_never_appears_in_smart_queue_listings(conn, client):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    for field in ("go_number", "go_date", "department"):
        _delete_field(conn, record_id, field)

    for queue in ops_review.SMART_QUEUES:
        response = client.get(f"/ops/review?queue={queue}")
        assert f'value="{record_id}"' not in response.text


# ---------------------------------------------------------------------------
# Bulk reject (Refinement 7)
# ---------------------------------------------------------------------------
def test_bulk_reject_requires_a_known_reason(client, conn):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    response = client.post(
        "/records/bulk-reject",
        data={"record_ids": [record_id], "reason": "not a real reason"},
    )
    assert response.status_code == 400


def test_bulk_reject_rejects_selected_records_and_writes_audit_trail(client, conn):
    from goengine import audit

    ids = [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id")]
    response = client.post(
        "/records/bulk-reject",
        data={"record_ids": ids[:2], "reason": "Duplicate", "queue": "extraction"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "bulk_rejected=2" in response.headers["location"]

    for record_id in ids[:2]:
        row = conn.execute("SELECT status, review_note FROM go_records WHERE id = ?", (record_id,)).fetchone()
        assert row["status"] == "rejected"
        assert row["review_note"] == "Duplicate"
        actions = [e.action for e in audit.trail(conn, entity_type="go_record", entity_id=record_id)]
        assert "record.rejected" in actions

    # untouched record stays pending
    row = conn.execute("SELECT status FROM go_records WHERE id = ?", (ids[2],)).fetchone()
    assert row["status"] == "pending"


def test_bulk_reject_skips_one_bad_id_without_failing_the_batch(client, conn):
    ids = [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id")]
    response = client.post(
        "/records/bulk-reject",
        data={"record_ids": [ids[0], 999999], "reason": "Other"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "bulk_rejected=1" in location
    assert "bulk_skipped=1" in location
    assert conn.execute("SELECT status FROM go_records WHERE id = ?", (ids[0],)).fetchone()["status"] == "rejected"


def test_likely_non_go_queue_suggests_a_reject_reason(client, conn):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    extraction_id = conn.execute(
        "SELECT extraction_id FROM go_records WHERE id = ?", (record_id,)
    ).fetchone()["extraction_id"]
    _delete_field(conn, record_id, "go_number")
    conn.execute(
        "UPDATE extraction_pages SET text = ? WHERE extraction_id = ? AND page_number = 1",
        ("This circular is issued to all departments for information.", extraction_id),
    )

    response = client.get("/ops/review?queue=likely_non_go")
    assert response.status_code == 200
    assert "Circular" in response.text


# ---------------------------------------------------------------------------
# Bulk reprocess (Refinement 3) + Critical Extraction Failure (Refinement 2)
# ---------------------------------------------------------------------------
def test_critical_extraction_failure_routed_to_failures_page(client, conn):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    for field in ("go_number", "go_date", "department"):
        _delete_field(conn, record_id, field)

    response = client.get("/ops/failures")
    assert response.status_code == 200
    assert f'value="{record_id}"' in response.text
    assert "Critical Extraction Failure" in response.text


def test_bulk_reprocess_creates_fresh_pending_records_and_preserves_originals(client, conn):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    for field in ("go_number", "go_date", "department"):
        _delete_field(conn, record_id, field)
    before_count = conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"]

    response = client.post(
        "/records/bulk-reprocess",
        data={"record_ids": [record_id], "redirect_to": "/ops/failures"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/ops/failures" in response.headers["location"]
    assert "bulk_reprocessed=1" in response.headers["location"]

    after_count = conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"]
    assert after_count == before_count + 1
    # original record untouched
    original = conn.execute("SELECT status FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert original["status"] == "pending"


def test_bulk_reprocess_skips_unknown_record_id_without_failing_the_batch(client, conn):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    response = client.post(
        "/records/bulk-reprocess",
        data={"record_ids": [record_id, 999999]},
        follow_redirects=False,
    )
    location = response.headers["location"]
    assert "bulk_reprocessed=1" in location
    assert "bulk_skipped=1" in location


def test_bulk_reprocess_redirect_to_only_accepts_local_paths(client, conn):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    response = client.post(
        "/records/bulk-reprocess",
        data={"record_ids": [record_id], "redirect_to": "https://evil.example/x"},
        follow_redirects=False,
    )
    assert response.headers["location"].startswith("/ops/failures")


# ---------------------------------------------------------------------------
# Bulk action safety: three-way Processed/Skipped/Failed (Refinement 8)
# ---------------------------------------------------------------------------
def test_bulk_approve_reports_three_way_summary(client, conn):
    ids = [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id")]
    _delete_field(conn, ids[0], "go_number")  # missing core field -> skipped, not approved

    response = client.post(
        "/records/bulk-approve",
        data={"record_ids": ids, "queue": "extraction"},
        follow_redirects=False,
    )
    location = response.headers["location"]
    assert "bulk_approved=2" in location
    assert "bulk_skipped=1" in location
    assert "bulk_failed=0" in location


# ---------------------------------------------------------------------------
# Page size (Refinement 2 of the original blueprint)
# ---------------------------------------------------------------------------
def test_review_hub_defaults_to_page_size_100(client):
    response = client.get("/ops/review?queue=extraction")
    assert response.status_code == 200
    assert '<option value="100" selected>100</option>' in response.text


def test_review_hub_accepts_configurable_page_sizes(client):
    for size in (50, 100, 250):
        response = client.get(f"/ops/review?queue=extraction&page_size={size}")
        assert response.status_code == 200


def test_review_hub_rejects_unsupported_page_size(client):
    response = client.get("/ops/review?queue=extraction&page_size=17")
    assert response.status_code == 400
