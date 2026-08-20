"""Production reset (operations/reset.py) and its backup export
(operations/backup.py). The one property that matters most here: documents
and document_blobs are never touched -- that's enforced by the schema
itself (documents_no_delete trigger), but these tests prove the *rest* of
the reset behaves correctly around that constraint: everything derived
from documents is cleared, config survives, and re-parsing the still-
archived documents afterward works exactly as claimed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from goengine.operations import backup as ops_backup
from goengine.operations import reset as ops_reset
from goengine.workbench.app import create_app
from tests.conftest import login_as


def test_backup_snapshot_includes_records_and_their_fields(conn, parsed_documents):
    from goengine import review

    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    review.approve(conn, record_id, reviewer="admin")

    snapshot = ops_backup.export_review_snapshot(conn)
    assert snapshot["record_count"] == 3
    approved = next(r for r in snapshot["records"] if r["id"] == record_id)
    assert approved["status"] == "approved"
    assert approved["reviewed_by"] == "admin"
    assert len(approved["fields"]) > 0
    assert any(f["field_name"] == "go_number" for f in approved["fields"])


def test_backup_snapshot_on_empty_dataset(conn):
    snapshot = ops_backup.export_review_snapshot(conn)
    assert snapshot["record_count"] == 0
    assert snapshot["records"] == []


# ---------------------------------------------------------------------------
# The reset itself
# ---------------------------------------------------------------------------
def test_reset_clears_go_records_but_preserves_documents(conn, parsed_documents):
    document_ids = parsed_documents
    result = ops_reset.reset_for_production(conn, actor="admin")

    assert result["go_records_removed"] == 3
    assert conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM go_fields").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM extractions").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM document_categories").fetchone()["n"] == 0

    # The documents themselves are completely untouched -- this is the one
    # guarantee the whole feature exists to uphold.
    surviving = conn.execute("SELECT id FROM documents ORDER BY id").fetchall()
    assert [int(r["id"]) for r in surviving] == sorted(document_ids)


def test_reset_preserves_configuration(conn, parsed_documents, source_id):
    from goengine.operations import auth as ops_auth
    from goengine.operations import geography

    ops_auth.create_user(conn, username="admin2", password="testpass123", role=ops_auth.ROLE_PLATFORM_ADMIN)
    state_id = geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    geography.add_district(conn, state_id=state_id, name="Chennai", code="CHE", actor="admin")
    conn.execute(
        "INSERT INTO system_settings (key, value) VALUES ('emailjs_service_id', 'keep-me')"
    )

    ops_reset.reset_for_production(conn, actor="admin")

    assert conn.execute("SELECT COUNT(*) AS n FROM sources WHERE id = ?", (source_id,)).fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM users WHERE username = 'admin2'").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM states WHERE code = 'TN'").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM districts WHERE code = 'CHE'").fetchone()["n"] == 1
    assert conn.execute(
        "SELECT value FROM system_settings WHERE key = 'emailjs_service_id'"
    ).fetchone()["value"] == "keep-me"


def test_reset_preserves_audit_log_and_golden_data(conn, parsed_documents):
    from goengine import audit
    from goengine.certification import golden

    golden_id = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    golden.annotate_field(conn, golden_id, "go_number", "GO-1-2026", annotator="alex")
    audit_count_before = len(audit.trail(conn, limit=10_000))

    ops_reset.reset_for_production(conn, actor="admin")

    assert conn.execute("SELECT COUNT(*) AS n FROM golden_documents").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM golden_annotations").fetchone()["n"] == 1
    # The reset itself adds an audit entry -- the trail only ever grows.
    assert len(audit.trail(conn, limit=10_000)) > audit_count_before


def test_reset_rolls_back_publication_status(conn, district_id_fixture, parsed_documents):
    from goengine import review
    from goengine.operations import geography
    from goengine.operations import publication as ops_publication

    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    review.approve(conn, record_id, reviewer="admin")
    conn.execute("UPDATE districts SET certification_status = 'CERTIFIED' WHERE id = ?", (district_id_fixture,))
    ops_publication.publish_district(conn, district_id_fixture, actor="admin")
    assert geography.get_district(conn, district_id_fixture).publication_status == "PUBLISHED"

    result = ops_reset.reset_for_production(conn, actor="admin")

    assert result["districts_reset"] == 1
    district = geography.get_district(conn, district_id_fixture)
    assert district.publication_status == "NOT_PUBLISHED"
    assert district.status == "CERTIFIED"  # rolled back to its certification, not left at PUBLISHED


def test_reset_moves_downloaded_documents_stubs_correctly(conn, parsed_documents):
    from goengine.discovery import crawler

    # One never-downloaded stub (safe to delete outright) alongside the
    # already-downloaded/parsed real documents from the fixture.
    conn.execute(
        """
        INSERT INTO discovered_documents (source_id, url, discovered_at, last_seen_at, status)
        SELECT source_id, 'https://cms.tn.gov.in/never-downloaded.pdf', datetime('now'), datetime('now'), 'new'
          FROM documents LIMIT 1
        """
    )
    parsed_status_before = {
        r["status"] for r in conn.execute("SELECT status FROM discovered_documents WHERE status != 'new'")
    }
    assert "parsed" in parsed_status_before or "verified" in parsed_status_before or parsed_status_before

    ops_reset.reset_for_production(conn, actor="admin")

    remaining_statuses = {r["status"] for r in conn.execute("SELECT status FROM discovered_documents")}
    assert "new" not in remaining_statuses  # the stub was deleted outright
    assert remaining_statuses <= {"downloaded"}  # everything else rolled back, none orphaned


def test_reset_is_idempotent(conn, parsed_documents):
    first = ops_reset.reset_for_production(conn, actor="admin")
    assert first["go_records_removed"] == 3

    second = ops_reset.reset_for_production(conn, actor="admin")
    assert second["go_records_removed"] == 0
    assert second["districts_reset"] == 0


def test_documents_can_be_reparsed_after_reset(conn, settings, parsed_documents):
    """The actual claim the feature is built on: nothing needs
    re-downloading after a reset, because the archived documents are
    untouched and the parse pipeline picks them straight back up."""
    from goengine.pipeline import run_parsing

    ops_reset.reset_for_production(conn, actor="admin")
    assert conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"] == 0

    report = run_parsing(conn, settings)
    assert report.succeeded == 3
    assert conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"] == 3


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
@pytest.fixture
def district_id_fixture(conn):
    from goengine.operations import geography

    state_id = geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    return geography.add_district(conn, state_id=state_id, name="Chennai", code="CHE", actor="admin")


@pytest.fixture
def client(conn, settings, parsed_documents):
    test_client = TestClient(create_app(settings))
    login_as(test_client, conn)
    return test_client


def test_backup_download_via_http(client):
    response = client.get("/ops/backup/download")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment" in response.headers["content-disposition"]
    body = response.json()
    assert body["record_count"] == 3


def test_backup_download_requires_permission(conn, settings, parsed_documents):
    client = TestClient(create_app(settings))
    login_as(client, conn, username="reviewer1", role="reviewer")
    response = client.get("/ops/backup/download")
    assert response.status_code == 403


def test_reset_page_shows_current_count(client):
    response = client.get("/ops/system-reset")
    assert response.status_code == 200
    assert "3 GO record" in response.text


def test_reset_via_http_requires_exact_confirmation_phrase(client, conn):
    wrong = client.post("/ops/system-reset/run", data={"confirm": "reset"})  # lowercase, must fail
    assert wrong.status_code == 400
    assert conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"] == 3  # untouched

    omitted = client.post("/ops/system-reset/run", data={})
    assert omitted.status_code == 422  # FastAPI's own required-field validation
    assert conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"] == 3  # untouched


def test_reset_via_http_succeeds_with_exact_phrase(client, conn):
    response = client.post("/ops/system-reset/run", data={"confirm": "RESET"})
    assert response.status_code == 200
    assert "Reset complete" in response.text
    assert conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == 3  # preserved


def test_reset_via_http_requires_manage_sources_permission(conn, settings, parsed_documents):
    client = TestClient(create_app(settings))
    login_as(client, conn, username="reviewer1", role="reviewer")
    response = client.post("/ops/system-reset/run", data={"confirm": "RESET"})
    assert response.status_code == 403
    assert conn.execute("SELECT COUNT(*) AS n FROM go_records").fetchone()["n"] == 3  # untouched
