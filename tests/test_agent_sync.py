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
