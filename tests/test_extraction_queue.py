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


def test_report_progress_returns_the_current_status(conn, agent_key):
    key_id, _ = agent_key
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    eq.claim_next(conn, agent_key_id=key_id)
    assert eq.report_progress(conn, rid, sources_total=1, sources_completed=0) == eq.STATUS_RUNNING


def test_report_progress_after_cancel_is_a_no_op_and_returns_the_terminal_status(conn, agent_key):
    """The actual mechanism a Cancel click relies on: once a request is
    terminal, a stray progress report from an agent that hasn't noticed yet
    must not resurrect it or overwrite "Cancelled by admin" -- and must tell
    the agent, via its return value, to stop."""
    key_id, _ = agent_key
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    eq.claim_next(conn, agent_key_id=key_id)
    eq.report_progress(conn, rid, sources_total=5, sources_completed=1)
    eq.cancel_request(conn, rid, actor="admin")

    status = eq.report_progress(conn, rid, sources_completed=2, documents_downloaded=99)
    assert status == eq.STATUS_FAILED

    row = eq.get_request(conn, rid)
    assert row["status"] == eq.STATUS_FAILED
    assert row["error"] == "Cancelled by admin"
    assert row["sources_completed"] == 1  # the post-cancel update was never applied
    assert row["documents_downloaded"] == 0  # column default -- never touched by the post-cancel update


def test_complete_request_after_cancel_does_not_overwrite_the_cancellation(conn, agent_key):
    """The agent's own eventual completion call (it may not notice the
    cancellation until its next progress report) must not clobber
    "Cancelled by admin" with a plain success or failure."""
    key_id, _ = agent_key
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    eq.claim_next(conn, agent_key_id=key_id)
    eq.cancel_request(conn, rid, actor="admin")

    eq.complete_request(conn, rid, ok=True)

    row = eq.get_request(conn, rid)
    assert row["status"] == eq.STATUS_FAILED
    assert row["error"] == "Cancelled by admin"


def test_resolve_local_source_ids_by_department(conn, source_id):
    ids = eq.resolve_local_source_ids(conn, state_name=None, district_name=None, department_filter=None)
    assert source_id in ids

    ids_health = eq.resolve_local_source_ids(
        conn, state_name=None, district_name=None, department_filter=["Health and Family Welfare"],
    )
    assert source_id not in ids_health  # the fixture source's department is "All Departments", not this


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


def test_progress_endpoint_reports_cancellation_to_the_agent(client, conn, agent_key):
    """The actual channel a Cancel click uses to reach a running agent: the
    /progress response's `status` field flips to FAILED the moment an admin
    cancels, even though the agent making this exact call has no idea yet --
    that's precisely what lets it notice within one report."""
    _, token = agent_key
    headers = {"Authorization": f"Bearer {token}"}
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    client.post("/api/agent/queue/claim", headers=headers)

    ok_response = client.post(f"/api/agent/queue/{rid}/progress", headers=headers, json={"sources_total": 1})
    assert ok_response.json()["status"] == eq.STATUS_RUNNING

    eq.cancel_request(conn, rid, actor="admin")

    cancelled_response = client.post(
        f"/api/agent/queue/{rid}/progress", headers=headers, json={"sources_completed": 1},
    )
    assert cancelled_response.status_code == 200
    assert cancelled_response.json()["status"] == eq.STATUS_FAILED


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
# Extraction Center UI: always enqueues a local request now -- cloud
# extraction was removed entirely (Render's network is blocked at the TCP
# level by TN government hosts, so it could never succeed in production).
# ---------------------------------------------------------------------------
def test_jobs_start_enqueues_a_local_request(client, conn):
    login_as(client, conn)
    res = client.post(
        "/ops/jobs/start", data={"departments": ["Health and Family Welfare"]}, follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/ops/jobs"
    assert eq.queue_size(conn) == 1


def test_jobs_start_ignores_a_stray_mode_field(client, conn):
    """No `mode` form field exists anymore -- posting one (e.g. a stale
    client/bookmarked form) must not resurrect the removed cloud path."""
    login_as(client, conn)
    res = client.post("/ops/jobs/start", data={"mode": "cloud"}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/ops/jobs"  # always the local-queue redirect now
    assert eq.queue_size(conn) == 1


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


# ---------------------------------------------------------------------------
# Phase 3.5 Initiative 4 -- Agent Operations Center
# ---------------------------------------------------------------------------
def test_agent_operations_status_with_no_requests(conn):
    status = eq.agent_operations_status(conn)
    assert status == {
        "running": False, "current_extraction": None,
        "last_run_at": None, "last_completed_at": None, "last_error": None,
    }


def test_agent_operations_status_reports_a_claimed_request_as_running(conn, agent_key):
    key_id, _ = agent_key
    rid = eq.enqueue_local_request(
        conn, state_id=None, district_id=None, department_filter=["Health and Family Welfare"], created_by="admin",
    )
    eq.claim_next(conn, agent_key_id=key_id)

    status = eq.agent_operations_status(conn)
    assert status["running"] is True
    assert status["current_extraction"]["request_id"] == rid
    assert status["current_extraction"]["department_filter"] == ["Health and Family Welfare"]


def test_agent_operations_status_reports_running_while_in_progress(conn, agent_key):
    key_id, _ = agent_key
    eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    rid = eq.claim_next(conn, agent_key_id=key_id)["id"]
    eq.report_progress(conn, rid, sources_total=2, sources_completed=1)

    status = eq.agent_operations_status(conn)
    assert status["running"] is True
    assert eq.get_request(conn, rid)["status"] == eq.STATUS_RUNNING


def test_agent_operations_status_tracks_last_run_completion_and_error(conn, agent_key):
    key_id, _ = agent_key

    first = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    eq.claim_next(conn, agent_key_id=key_id)
    eq.report_progress(conn, first, sources_total=1, sources_completed=1)
    eq.complete_request(conn, first, ok=True)

    status_after_success = eq.agent_operations_status(conn)
    assert status_after_success["running"] is False
    assert status_after_success["last_completed_at"] is not None
    assert status_after_success["last_error"] is None

    second = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    eq.claim_next(conn, agent_key_id=key_id)
    eq.complete_request(conn, second, ok=False, error="no local sources matched")

    status_after_failure = eq.agent_operations_status(conn)
    assert status_after_failure["last_error"] == "no local sources matched"
    assert status_after_failure["last_run_at"] is not None


# ---------------------------------------------------------------------------
# Phase 3.5 Initiative 5 -- Extraction Run History
# ---------------------------------------------------------------------------
def test_run_history_row_computes_duration(conn):
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    conn.execute(
        "UPDATE extraction_requests SET started_at = ?, finished_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:05:30+00:00", rid),
    )
    result = eq.run_history_row(conn, eq.get_request(conn, rid))
    assert result["duration_seconds"] == 330.0


def test_run_history_row_duration_is_none_before_the_run_finishes(conn):
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    result = eq.run_history_row(conn, eq.get_request(conn, rid))
    assert result["duration_seconds"] is None
    assert result["documents_published"] is None


def test_run_history_row_documents_published_is_none_for_resync_all(conn):
    rid = eq.enqueue_resync_all_request(conn, created_by="admin")
    conn.execute(
        "UPDATE extraction_requests SET started_at = ?, finished_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:05:00+00:00", rid),
    )
    result = eq.run_history_row(conn, eq.get_request(conn, rid))
    assert result["documents_published"] is None


def test_run_history_row_counts_only_approved_documents_in_window_and_scope(conn, settings, fetcher, source_id):
    from goengine import review
    from goengine.pipeline import run_all

    run_all(conn, settings, fetcher, only_due=False)
    record_ids = [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]
    review.approve(conn, record_ids[0], reviewer="admin")
    # record_ids[1] and [2] stay pending -- must not count as "published".

    window = conn.execute("SELECT MIN(downloaded_at) AS start, MAX(downloaded_at) AS end FROM documents").fetchone()
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    conn.execute(
        "UPDATE extraction_requests SET started_at = ?, finished_at = ? WHERE id = ?",
        (window["start"], window["end"], rid),
    )
    result = eq.run_history_row(conn, eq.get_request(conn, rid))
    assert result["documents_published"] == 1


def test_run_history_row_excludes_documents_outside_the_run_scope(conn, settings, fetcher, source_id):
    from goengine import review
    from goengine.pipeline import run_all

    run_all(conn, settings, fetcher, only_due=False)
    record_ids = [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]
    review.approve(conn, record_ids[0], reviewer="admin")

    window = conn.execute("SELECT MIN(downloaded_at) AS start, MAX(downloaded_at) AS end FROM documents").fetchone()
    # source_id's department is "All Departments" -- a filter naming a real,
    # different department must exclude it entirely.
    rid = eq.enqueue_local_request(
        conn, state_id=None, district_id=None, department_filter=["Health and Family Welfare"], created_by="admin",
    )
    conn.execute(
        "UPDATE extraction_requests SET started_at = ?, finished_at = ? WHERE id = ?",
        (window["start"], window["end"], rid),
    )
    result = eq.run_history_row(conn, eq.get_request(conn, rid))
    assert result["documents_published"] == 0


# ---------------------------------------------------------------------------
# Cancelling a stuck/no-longer-wanted request -- a real production incident:
# a local agent hung mid-crawl and Ctrl+C in its terminal did nothing (it
# only checks for interruption between whole requests), leaving a request
# stuck at RUNNING forever with no way to clear it from the dashboard.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["QUEUED", "CLAIMED", "RUNNING"])
def test_cancel_request_marks_a_cancellable_request_as_failed(conn, agent_key, status):
    key_id, _ = agent_key
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    if status != "QUEUED":
        eq.claim_next(conn, agent_key_id=key_id)
    if status == "RUNNING":
        eq.report_progress(conn, rid, sources_total=1, sources_completed=0)
    assert eq.get_request(conn, rid)["status"] == status

    eq.cancel_request(conn, rid, actor="admin")

    row = eq.get_request(conn, rid)
    assert row["status"] == eq.STATUS_FAILED
    assert row["error"] == "Cancelled by admin"
    assert row["finished_at"] is not None


def test_cancel_request_refuses_an_already_finished_request(conn):
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    eq.complete_request(conn, rid, ok=True)

    with pytest.raises(ValueError, match="not cancellable"):
        eq.cancel_request(conn, rid, actor="admin")


def test_cancel_request_unknown_id_raises(conn):
    with pytest.raises(LookupError):
        eq.cancel_request(conn, 9999, actor="admin")


def test_cancel_request_records_an_audit_entry_with_the_admin_as_actor(conn):
    from goengine import audit

    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    eq.cancel_request(conn, rid, actor="alex")

    entries = audit.trail(conn, entity_type="extraction_request", entity_id=rid)
    cancelled = [e for e in entries if e.action == "extraction_request.cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0].actor == "alex"


def test_jobs_cancel_route_marks_request_failed_and_redirects(client, conn):
    login_as(client, conn)
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")

    response = client.post(f"/ops/jobs/{rid}/cancel", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ops/jobs"
    assert eq.get_request(conn, rid)["status"] == eq.STATUS_FAILED


def test_jobs_cancel_route_404s_for_an_unknown_request(client, conn):
    login_as(client, conn)
    assert client.post("/ops/jobs/9999/cancel").status_code == 404


def test_jobs_cancel_route_400s_for_an_already_finished_request(client, conn):
    login_as(client, conn)
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")
    eq.complete_request(conn, rid, ok=True)

    assert client.post(f"/ops/jobs/{rid}/cancel").status_code == 400


def test_jobs_cancel_route_requires_certify_permission(conn, settings):
    from goengine.operations import auth as ops_auth

    app = create_app(settings)
    reviewer_client = TestClient(app)
    login_as(reviewer_client, conn, username="reviewer1", role=ops_auth.ROLE_REVIEWER)
    rid = eq.enqueue_local_request(conn, state_id=None, district_id=None, department_filter=None, created_by="admin")

    assert reviewer_client.post(f"/ops/jobs/{rid}/cancel").status_code == 403
