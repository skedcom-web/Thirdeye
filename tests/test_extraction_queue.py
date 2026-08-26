import json

import pytest
from fastapi.testclient import TestClient

from goengine.operations import agent_auth, extraction_queue as eq
from goengine.workbench.app import create_app
from tests.conftest import login_as


@pytest.fixture
def client(conn, settings):
    return TestClient(create_app(settings))


@pytest.fixture
def agent_key(conn):
    key_id, token = agent_auth.generate_key(conn, label="Test Agent", created_by="admin")
    return key_id, token


def test_enqueue_and_claim(conn, agent_key):
    key_id, _ = agent_key
    rid = eq.enqueue_local_request(
        conn, state_id=None, district_id=None, department_filter=["health"], created_by="admin"
    )
    assert eq.queue_size(conn) == 1

    claimed = eq.claim_next(conn, agent_key_id=key_id)
    assert claimed is not None
    assert claimed["id"] == rid
    assert claimed["status"] == eq.STATUS_CLAIMED

    payload = eq.claim_payload(conn, claimed)
    assert payload["department_filter"] == ["health"]


def test_claim_race_second_agent_gets_nothing(conn, agent_key):
    key_id, _ = agent_key
    other_id, _ = agent_auth.generate_key(conn, label="Other Agent", created_by="admin")
    eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")

    first = eq.claim_next(conn, agent_key_id=key_id)
    assert first is not None
    second = eq.claim_next(conn, agent_key_id=other_id)
    assert second is None  # already claimed


def test_progress_transitions_to_running_then_complete(conn, agent_key):
    key_id, _ = agent_key
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    eq.claim_next(conn, agent_key_id=key_id)

    eq.report_progress(conn, rid, sources_total=3, sources_completed=1)
    row = eq.get_request(conn, rid)
    assert row["status"] == eq.STATUS_RUNNING
    assert row["started_at"] is not None
    assert row["sources_completed"] == 1

    eq.complete_request(conn, rid, ok=True)
    row2 = eq.get_request(conn, rid)
    assert row2["status"] == eq.STATUS_COMPLETED
    assert row2["finished_at"] is not None
    assert eq.queue_size(conn) == 0


def test_complete_with_failure_records_error(conn, agent_key):
    key_id, _ = agent_key
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    eq.claim_next(conn, agent_key_id=key_id)
    eq.complete_request(conn, rid, ok=False, error="no local sources matched")
    row = eq.get_request(conn, rid)
    assert row["status"] == eq.STATUS_FAILED
    assert row["error"] == "no local sources matched"


def test_resolve_local_source_ids_by_department(conn, source_id):
    ids = eq.resolve_local_source_ids(conn, state_name=None, district_name=None, department_filter=None)
    assert source_id in ids

    ids_health = eq.resolve_local_source_ids(conn, state_name=None, district_name=None, department_filter=["health"])
    assert source_id not in ids_health  # the fixture source's department doesn't bucket into health


def test_resolve_local_source_ids_unknown_state_name_returns_empty(conn, source_id):
    ids = eq.resolve_local_source_ids(
        conn, state_name="Nonexistent State", district_name=None, department_filter=None
    )
    assert ids == []


# ---------------------------------------------------------------------------
# Agent-facing HTTP endpoints
# ---------------------------------------------------------------------------
def test_agent_sources_endpoint(client, conn, agent_key, source_id):
    _, token = agent_key
    res = client.get("/api/agent/sources", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    names = [s["name"] for s in res.json()]
    assert "Tamil Nadu GO Portal" in names


def test_queue_claim_progress_complete_http(client, conn, agent_key):
    _, token = agent_key
    headers = {"Authorization": f"Bearer {token}"}
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=["health"], created_by="admin")

    res = client.post("/api/agent/queue/claim", headers=headers)
    assert res.status_code == 200
    claimed = res.json()["request"]
    assert claimed["id"] == rid

    res2 = client.post("/api/agent/queue/claim", headers=headers)
    assert res2.json()["request"] is None

    res3 = client.post(f"/api/agent/queue/{rid}/progress", headers=headers, json={"sources_total": 2, "sources_completed": 1})
    assert res3.status_code == 200

    res4 = client.post(f"/api/agent/queue/{rid}/complete", headers=headers, json={"ok": True, "documents_downloaded": 5})
    assert res4.status_code == 200

    row = eq.get_request(conn, rid)
    assert row["status"] == eq.STATUS_COMPLETED
    assert row["documents_downloaded"] == 5


def test_queue_progress_requires_claiming_agent(client, conn, agent_key):
    _, token = agent_key
    other_id, other_token = agent_auth.generate_key(conn, label="Impostor", created_by="admin")
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    client.post("/api/agent/queue/claim", headers={"Authorization": f"Bearer {token}"})

    res = client.post(
        f"/api/agent/queue/{rid}/complete",
        headers={"Authorization": f"Bearer {other_token}"}, json={"ok": True},
    )
    assert res.status_code == 403


def test_queue_endpoints_require_auth(client, conn, agent_key):
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    assert client.post("/api/agent/queue/claim").status_code == 401
    assert client.post(f"/api/agent/queue/{rid}/progress", json={}).status_code == 401
    assert client.get("/api/agent/sources").status_code == 401


# ---------------------------------------------------------------------------
# Extraction Center UI: mode selector -> enqueue instead of cloud job
# ---------------------------------------------------------------------------
def test_jobs_start_local_mode_enqueues_instead_of_running(client, conn):
    login_as(client, conn)
    res = client.post("/ops/jobs/start", data={"mode": "local", "departments": ["health"]}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/ops/jobs"
    assert eq.queue_size(conn) == 1

    from goengine.operations import jobs as ops_jobs
    assert ops_jobs.list_jobs(conn) == []  # no cloud job was started


def test_jobs_start_cloud_mode_unchanged(client, conn):
    login_as(client, conn)
    res = client.post("/ops/jobs/start", data={"mode": "cloud"}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/ops/jobs/")
    assert res.headers["location"] != "/ops/jobs"


def test_extraction_center_shows_agent_offline_by_default(client, conn):
    login_as(client, conn)
    res = client.get("/ops/jobs")
    assert res.status_code == 200
    assert "Offline" in res.text


def test_extraction_center_shows_agent_online_after_recent_use(client, conn, agent_key):
    _, token = agent_key
    client.get("/api/agent/sources", headers={"Authorization": f"Bearer {token}"})  # updates last_used_at
    login_as(client, conn)
    res = client.get("/ops/jobs")
    assert "Online" in res.text


# ---------------------------------------------------------------------------
# Resync-all requests (recovering local agent sync bookkeeping after a
# server-side reset -- see operations/reset.py and cli.py's
# _run_resync_all_request)
# ---------------------------------------------------------------------------
def test_enqueue_resync_all_creates_scopeless_request(conn, agent_key):
    key_id, _ = agent_key
    rid = eq.enqueue_resync_all_request(conn, created_by="admin")
    claimed = eq.claim_next(conn, agent_key_id=key_id)
    assert claimed["id"] == rid

    payload = eq.claim_payload(conn, claimed)
    assert payload["kind"] == eq.KIND_RESYNC_ALL
    assert "department_filter" not in payload


def test_enqueue_resync_all_does_not_duplicate_pending_request(conn):
    rid1 = eq.enqueue_resync_all_request(conn, created_by="admin")
    rid2 = eq.enqueue_resync_all_request(conn, created_by="admin")
    assert rid1 == rid2
    assert eq.queue_size(conn) == 1


def test_enqueue_resync_all_after_completion_creates_new_request(conn, agent_key):
    key_id, _ = agent_key
    rid1 = eq.enqueue_resync_all_request(conn, created_by="admin")
    eq.claim_next(conn, agent_key_id=key_id)
    eq.complete_request(conn, rid1, ok=True)

    rid2 = eq.enqueue_resync_all_request(conn, created_by="admin")
    assert rid2 != rid1


def test_normal_extraction_request_defaults_to_extraction_kind(conn, agent_key):
    key_id, _ = agent_key
    rid = eq.enqueue_local_request(
        conn, state_id=None, district_id=None, department_filter=["health"], created_by="admin"
    )
    claimed = eq.claim_next(conn, agent_key_id=key_id)
    payload = eq.claim_payload(conn, claimed)
    assert payload["id"] == rid
    assert payload["kind"] == eq.KIND_EXTRACTION
    assert payload["department_filter"] == ["health"]


def test_jobs_resync_all_route_enqueues(client, conn):
    login_as(client, conn)
    res = client.post("/ops/jobs/resync-all", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/ops/jobs"
    assert eq.queue_size(conn) == 1
    rows = eq.list_requests(conn)
    assert rows[0]["kind"] == eq.KIND_RESYNC_ALL


def test_production_reset_auto_queues_resync_all(conn):
    from goengine.operations import reset as ops_reset

    result = ops_reset.reset_for_production(conn, actor="admin")
    assert result["resync_request_id"] is not None
    rows = eq.list_requests(conn)
    assert any(r["kind"] == eq.KIND_RESYNC_ALL for r in rows)
