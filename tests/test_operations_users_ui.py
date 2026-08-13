"""Module 11 -- User & Role Management admin UI (over auth.py)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from goengine.operations import auth
from goengine.workbench.app import create_app
from tests.conftest import login_as


@pytest.fixture
def client(conn, settings):
    test_client = TestClient(create_app(settings))
    login_as(test_client, conn)  # bootstrap platform_admin "admin"
    return test_client


def test_users_list_renders(client):
    response = client.get("/ops/users")
    assert response.status_code == 200
    assert "admin" in response.text
    assert "(you)" in response.text


def test_add_user_via_http(client, conn):
    response = client.post(
        "/ops/users/add", data={"username": "reviewer1", "password": "reviewerpass1", "role": "reviewer"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert auth.authenticate(conn, "reviewer1", "reviewerpass1") is not None


def test_add_state_admin_requires_a_state(client):
    response = client.post(
        "/ops/users/add", data={"username": "tn_admin", "password": "adminpass12", "role": "state_admin"},
    )
    assert response.status_code == 400


def test_deactivate_and_reactivate_user_via_http(client, conn):
    client.post("/ops/users/add", data={"username": "x", "password": "xpassword1", "role": "read_only"})
    user_id = conn.execute("SELECT id FROM users WHERE username = 'x'").fetchone()["id"]

    deactivate = client.post(f"/ops/users/{user_id}/active", data={}, follow_redirects=False)
    assert deactivate.status_code == 303
    assert auth.authenticate(conn, "x", "xpassword1") is None

    reactivate = client.post(f"/ops/users/{user_id}/active", data={"active": "1"}, follow_redirects=False)
    assert reactivate.status_code == 303
    assert auth.authenticate(conn, "x", "xpassword1") is not None


def test_cannot_deactivate_own_account(client, conn):
    admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
    response = client.post(f"/ops/users/{admin_id}/active", data={})
    assert response.status_code == 400
    assert auth.authenticate(conn, "admin", "testpass123") is not None  # still active


def test_non_admin_cannot_manage_users(conn, settings):
    client = TestClient(create_app(settings))
    login_as(client, conn, username="reviewer1", role="reviewer")

    response = client.post(
        "/ops/users/add", data={"username": "y", "password": "ypassword1", "role": "read_only"}
    )
    assert response.status_code == 403


def test_user_creation_is_audited(client, conn):
    from goengine import audit

    client.post("/ops/users/add", data={"username": "reviewer1", "password": "reviewerpass1", "role": "reviewer"})
    user_id = conn.execute("SELECT id FROM users WHERE username = 'reviewer1'").fetchone()["id"]

    entries = audit.trail(conn, entity_type="user", entity_id=user_id)
    assert any(e.action == "user.created" and e.actor == "admin" for e in entries)
