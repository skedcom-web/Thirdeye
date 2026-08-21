import json

import pytest
from fastapi.testclient import TestClient

from goengine.operations import agent_auth
from goengine.registry import add_source
from goengine.sampledata import write_samples
from goengine.workbench.app import create_app
from tests.conftest import login_as


@pytest.fixture
def client(conn, settings):
    return TestClient(create_app(settings))


@pytest.fixture
def sample_pdf(tmp_path):
    written = write_samples(tmp_path / "agent-samples")
    sample, path = written[0]
    return sample, path


def test_generate_and_verify_key(conn):
    key_id, token = agent_auth.generate_key(conn, label="Laptop", created_by="admin")
    ak = agent_auth.verify_key(conn, token)
    assert ak is not None
    assert ak.id == key_id
    assert ak.label == "Laptop"
    assert ak.active
    assert agent_auth.verify_key(conn, "tea_wrong") is None


def test_revoked_key_rejected(conn):
    _, token = agent_auth.generate_key(conn, label="Laptop", created_by="admin")
    key_id, _ = agent_auth.generate_key(conn, label="Laptop2", created_by="admin")
    agent_auth.revoke_key(conn, key_id, actor="admin")
    revoked = agent_auth.verify_key(conn, token)
    assert revoked is not None  # different key, untouched

    _, token2 = agent_auth.generate_key(conn, label="ToRevoke", created_by="admin")
    ak2 = agent_auth.verify_key(conn, token2)
    agent_auth.revoke_key(conn, ak2.id, actor="admin")
    assert agent_auth.verify_key(conn, token2) is None


def test_sync_endpoint_requires_bearer_token(client, conn):
    res = client.post("/api/agent/sync/document", files={"file": ("x.pdf", b"%PDF-1.4")}, data={"payload": "{}"})
    assert res.status_code == 401

    res = client.post(
        "/api/agent/sync/document",
        files={"file": ("x.pdf", b"%PDF-1.4")}, data={"payload": "{}"},
        headers={"Authorization": "Bearer bogus"},
    )
    assert res.status_code == 401


def test_sync_endpoint_rejects_unapproved_source_url(client, conn, source_id):
    _, token = agent_auth.generate_key(conn, label="Laptop", created_by="admin")
    payload = json.dumps({"source_id": source_id, "source_url": "https://evil.example.com/go.pdf"})
    res = client.post(
        "/api/agent/sync/document",
        files={"file": ("x.pdf", b"%PDF-1.4")}, data={"payload": payload},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 400
    assert "not an approved government source" in res.text


def test_sync_endpoint_archives_and_parses_digital_pdf(client, conn, source_id, sample_pdf):
    sample, path = sample_pdf
    _, token = agent_auth.generate_key(conn, label="Laptop", created_by="admin")
    payload = json.dumps({
        "source_id": source_id,
        "source_url": "https://cms.tn.gov.in/sites/default/files/go/sample.pdf",
        "link_text": f"{sample.go_number} abstract",
        "file_name": path.name,
        "ocr_pages": [],
    })
    res = client.post(
        "/api/agent/sync/document",
        files={"file": (path.name, path.read_bytes(), "application/pdf")},
        data={"payload": payload},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["is_new_version"] is True
    assert body["already_synced"] is False
    assert body["document_id"] is not None
    assert body["go_record_id"] is not None

    doc = conn.execute("SELECT * FROM documents WHERE id = ?", (body["document_id"],)).fetchone()
    assert doc is not None
    assert doc["sha256"]

    log = conn.execute("SELECT * FROM agent_sync_log WHERE document_id = ?", (body["document_id"],)).fetchone()
    assert log is not None
    assert log["ok"] == 1


def test_sync_endpoint_idempotent_retry(client, conn, source_id, sample_pdf):
    sample, path = sample_pdf
    _, token = agent_auth.generate_key(conn, label="Laptop", created_by="admin")
    payload = json.dumps({
        "source_id": source_id,
        "source_url": "https://cms.tn.gov.in/sites/default/files/go/sample2.pdf",
        "file_name": path.name,
        "ocr_pages": [],
    })
    file_bytes = path.read_bytes()

    res1 = client.post(
        "/api/agent/sync/document",
        files={"file": (path.name, file_bytes, "application/pdf")}, data={"payload": payload},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res1.status_code == 200
    doc_count_1 = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]

    res2 = client.post(
        "/api/agent/sync/document",
        files={"file": (path.name, file_bytes, "application/pdf")}, data={"payload": payload},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res2.status_code == 200
    body2 = res2.json()
    assert body2["already_synced"] is True
    assert body2["is_new_version"] is False
    assert body2["document_id"] == res1.json()["document_id"]

    doc_count_2 = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    assert doc_count_2 == doc_count_1  # no duplicate row

    # A real production recovery (retrying a sync that had already
    # succeeded, repeatedly, across a multi-day migration) exposed the
    # actual bug here: the documents row was correctly deduplicated, but
    # extraction re-ran on every retry regardless, quietly piling up a
    # brand-new go_records row per retry for the exact same content --
    # inflating the review queue with duplicates of documents that were
    # never actually re-extracted.
    document_id = res1.json()["document_id"]
    assert res2.json()["go_record_id"] == res1.json()["go_record_id"]
    record_count = conn.execute(
        "SELECT COUNT(*) AS n FROM go_records WHERE document_id = ?", (document_id,)
    ).fetchone()["n"]
    assert record_count == 1


def test_resync_with_new_ocr_data_recovers_a_previously_failed_document(client, conn, source_id):
    """A real production bug: a document with no digital text layer (a
    scan) synced once with no OCR data attached (the local agent hadn't
    parsed it yet), landing as needs_ocr=1 with every core field NOT_FOUND.
    Fixing the local gap and resyncing the exact same bytes -- but this
    time with real OCR text -- must actually apply the correction, not get
    silently swallowed by the "unchanged bytes, reuse the existing record"
    guard that exists specifically to stop *wasteful* duplicate resyncs."""
    import pymupdf

    _, token = agent_auth.generate_key(conn, label="Laptop", created_by="admin")
    blank_pdf = pymupdf.open()
    blank_pdf.new_page()
    file_bytes = blank_pdf.tobytes()

    payload_no_ocr = json.dumps({
        "source_id": source_id,
        "source_url": "https://cms.tn.gov.in/sites/default/files/go/scan.pdf",
        "file_name": "scan.pdf",
        "ocr_pages": [],
    })
    first = client.post(
        "/api/agent/sync/document",
        files={"file": ("scan.pdf", file_bytes, "application/pdf")}, data={"payload": payload_no_ocr},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert first.status_code == 200
    first_record_id = first.json()["go_record_id"]
    first_row = conn.execute(
        "SELECT status, needs_ocr FROM go_records r JOIN extractions e ON e.id = r.extraction_id WHERE r.id = ?",
        (first_record_id,),
    ).fetchone()
    assert first_row["needs_ocr"] == 1  # confirms the bug scenario actually reproduced

    payload_with_ocr = json.dumps({
        "source_id": source_id,
        "source_url": "https://cms.tn.gov.in/sites/default/files/go/scan.pdf",
        "file_name": "scan.pdf",
        "ocr_pages": [
            {
                "page_number": 1,
                "text": "G.O.(Ms) No.319 Dated: 14.07.2025\nHealth and Family Welfare Department",
                "mean_confidence": 0.9,
            }
        ],
    })
    second = client.post(
        "/api/agent/sync/document",
        files={"file": ("scan.pdf", file_bytes, "application/pdf")}, data={"payload": payload_with_ocr},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert second.status_code == 200
    document_id = second.json()["document_id"]
    second_record_id = second.json()["go_record_id"]
    assert second_record_id != first_record_id  # a real correction, not the stale reused record

    fields = {
        r["field_name"]: r["normalized_value"]
        for r in conn.execute("SELECT field_name, normalized_value FROM go_fields WHERE record_id = ?", (second_record_id,))
    }
    assert "319" in fields["go_number"]
    assert fields["department"] == "Health and Family Welfare"

    # The stale, empty-text record from the first sync must be gone --
    # otherwise this "fix" just reintroduces the duplicate-record bug.
    remaining = [
        int(r["id"]) for r in conn.execute("SELECT id FROM go_records WHERE document_id = ?", (document_id,))
    ]
    assert remaining == [second_record_id]


def test_resync_without_new_ocr_data_still_reuses_the_existing_record(client, conn, source_id):
    """The guard this recovery path lives inside must still do its job:
    resyncing a document that's already fine (or still has nothing new to
    offer) must not re-parse it every time."""
    _, token = agent_auth.generate_key(conn, label="Laptop", created_by="admin")
    blank_pdf = __import__("pymupdf").open()
    blank_pdf.new_page()
    file_bytes = blank_pdf.tobytes()
    payload = json.dumps({
        "source_id": source_id,
        "source_url": "https://cms.tn.gov.in/sites/default/files/go/scan2.pdf",
        "file_name": "scan2.pdf",
        "ocr_pages": [],
    })
    first = client.post(
        "/api/agent/sync/document",
        files={"file": ("scan2.pdf", file_bytes, "application/pdf")}, data={"payload": payload},
        headers={"Authorization": f"Bearer {token}"},
    )
    second = client.post(
        "/api/agent/sync/document",
        files={"file": ("scan2.pdf", file_bytes, "application/pdf")}, data={"payload": payload},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert second.json()["go_record_id"] == first.json()["go_record_id"]
    document_id = first.json()["document_id"]
    count = conn.execute("SELECT COUNT(*) AS n FROM go_records WHERE document_id = ?", (document_id,)).fetchone()["n"]
    assert count == 1


def test_agents_ui_generate_and_revoke(client, conn):
    login_as(client, conn)
    res_add = client.post("/ops/agents/add", data={"label": "Ramesh's laptop"}, follow_redirects=False)
    assert res_add.status_code == 200
    assert "Ramesh&#39;s laptop" in res_add.text or "Ramesh's laptop" in res_add.text

    key_row = conn.execute("SELECT id FROM agent_keys ORDER BY id DESC LIMIT 1").fetchone()
    key_id = key_row["id"]

    res_list = client.get("/ops/agents")
    assert res_list.status_code == 200
    assert "Ramesh" in res_list.text

    res_revoke = client.post(f"/ops/agents/{key_id}/revoke", follow_redirects=False)
    assert res_revoke.status_code == 303

    res_list2 = client.get("/ops/agents")
    assert "Revoked" in res_list2.text or "revoked" in res_list2.text.lower()


def test_agents_page_requires_manage_users_permission(client, conn):
    login_as(client, conn, username="reviewer1", role="reviewer")
    res = client.get("/ops/agents")
    assert res.status_code == 403
