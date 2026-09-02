"""Phase 3.9 Initiatives 6 & 7 -- Republish workflow HTTP surface."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from goengine import review
from goengine.operations import auth as ops_auth
from goengine.operations import republish
from goengine.pipeline import run_all
from goengine.workbench.app import create_app
from tests.conftest import login_as


@pytest.fixture
def client(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    conn.commit()
    test_client = TestClient(create_app(settings))
    login_as(test_client, conn)
    return test_client


@pytest.fixture
def approved_record_id(conn):
    record_id = int(conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"])
    review.approve(conn, record_id, reviewer="admin")
    return record_id


def _revision_form(subject: str = "", budget: str = "", district: str = "", scheme_name: str = "", reason: str = "typo fix"):
    return {
        "field_name": ["subject", "budget", "district", "scheme_name"],
        "new_value": [subject, budget, district, scheme_name],
        "reason": reason,
    }


def test_request_revision_via_http(client, conn, approved_record_id):
    response = client.post(
        f"/records/{approved_record_id}/revisions",
        data=_revision_form(subject="Corrected subject"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    history = republish.revision_history(conn, approved_record_id)
    assert len(history) == 1
    assert len(history[0]["changes"]) == 1
    assert history[0]["changes"][0]["field_name"] == "subject"
    assert history[0]["changes"][0]["new_value"] == "Corrected subject"

    page = client.get(f"/records/{approved_record_id}")
    assert "Revision History" in page.text
    assert "REVISION_DRAFT" in page.text


def test_request_revision_on_unapproved_record_shows_inline_error(client, conn):
    record_id = int(conn.execute("SELECT MIN(id) AS id FROM go_records WHERE status = 'pending'").fetchone()["id"])
    response = client.post(
        f"/records/{record_id}/revisions", data=_revision_form(subject="x"), follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    followed = client.get(response.headers["location"])
    assert "published" in followed.text


def test_full_revision_lifecycle_via_http(client, conn, approved_record_id):
    client.post(f"/records/{approved_record_id}/revisions", data=_revision_form(subject="New subject via HTTP"))
    revision_id = republish.revision_history(conn, approved_record_id)[0]["id"]

    submit = client.post(f"/records/{approved_record_id}/revisions/{revision_id}/submit", follow_redirects=False)
    assert submit.status_code == 303
    assert republish.revision_history(conn, approved_record_id)[0]["status"] == republish.STATUS_PENDING_REVIEW

    # admin requested it (login_as's default user), so admin approving it
    # must be refused -- the successful cross-reviewer approval path is
    # covered separately by test_approve_by_a_different_reviewer_republishes.
    self_approve = client.post(
        f"/records/{approved_record_id}/revisions/{revision_id}/approve", follow_redirects=False,
    )
    assert self_approve.status_code == 303
    assert "error=" in self_approve.headers["location"]
    assert republish.revision_history(conn, approved_record_id)[0]["status"] == republish.STATUS_PENDING_REVIEW


def test_approve_by_a_different_reviewer_republishes(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    conn.commit()
    client = TestClient(create_app(settings))
    login_as(client, conn, username="alex", role=ops_auth.ROLE_REVIEWER)
    record_id = int(conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"])
    review.approve(conn, record_id, reviewer="alex")

    client.post(f"/records/{record_id}/revisions", data=_revision_form(subject="Reviewed by someone else"))
    revision_id = republish.revision_history(conn, record_id)[0]["id"]

    reviewer_client = TestClient(create_app(settings))
    login_as(reviewer_client, conn, username="morgan", role=ops_auth.ROLE_REVIEWER)

    approve = reviewer_client.post(
        f"/records/{record_id}/revisions/{revision_id}/approve", follow_redirects=False,
    )
    assert approve.status_code == 303
    assert "error=" not in approve.headers["location"]

    entry = republish.revision_history(conn, record_id)[0]
    assert entry["status"] == republish.STATUS_REPUBLISHED

    page = reviewer_client.get(f"/records/{record_id}")
    assert "REPUBLISHED" in page.text


def test_reject_revision_via_http(client, conn, approved_record_id):
    client.post(f"/records/{approved_record_id}/revisions", data=_revision_form(subject="Should be rejected"))
    revision_id = republish.revision_history(conn, approved_record_id)[0]["id"]

    response = client.post(
        f"/records/{approved_record_id}/revisions/{revision_id}/reject",
        data={"reason": "not needed"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    entry = republish.revision_history(conn, approved_record_id)[0]
    assert entry["status"] == republish.STATUS_REJECTED
