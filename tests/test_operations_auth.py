"""Module 11 -- User & Role Management: password hashing, sessions, RBAC."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from goengine.operations import auth
from goengine.workbench.app import create_app
from tests.conftest import login_as


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def test_password_hash_verifies_correctly():
    hashed = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", hashed)
    assert not auth.verify_password("wrong password", hashed)


def test_password_hashes_are_salted_differently():
    a = auth.hash_password("samepassword123")
    b = auth.hash_password("samepassword123")
    assert a != b  # different random salts
    assert auth.verify_password("samepassword123", a)
    assert auth.verify_password("samepassword123", b)


def test_verify_password_rejects_malformed_stored_value():
    assert not auth.verify_password("anything", "not-a-real-hash")
    assert not auth.verify_password("anything", "")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def test_create_user_and_authenticate(conn):
    auth.create_user(conn, username="alex", password="longenough1", role=auth.ROLE_REVIEWER, actor="admin")
    assert auth.authenticate(conn, "alex", "longenough1") is not None
    assert auth.authenticate(conn, "alex", "wrongpassword") is None
    assert auth.authenticate(conn, "nobody", "longenough1") is None


def test_duplicate_username_rejected(conn):
    auth.create_user(conn, username="alex", password="longenough1", role=auth.ROLE_REVIEWER, actor="admin")
    with pytest.raises(auth.AuthError, match="already taken"):
        auth.create_user(conn, username="alex", password="different1", role=auth.ROLE_AUDITOR, actor="admin")


def test_short_password_rejected(conn):
    with pytest.raises(auth.AuthError, match="8 characters"):
        auth.create_user(conn, username="alex", password="short", role=auth.ROLE_REVIEWER, actor="admin")


def test_invalid_role_rejected(conn):
    with pytest.raises(auth.AuthError):
        auth.create_user(conn, username="alex", password="longenough1", role="superuser", actor="admin")


def test_state_admin_requires_a_state(conn):
    with pytest.raises(auth.AuthError, match="assigned a state"):
        auth.create_user(conn, username="alex", password="longenough1", role=auth.ROLE_STATE_ADMIN, actor="admin")


def test_deactivated_user_cannot_authenticate(conn):
    auth.create_user(conn, username="alex", password="longenough1", role=auth.ROLE_REVIEWER, actor="admin")
    user = auth.authenticate(conn, "alex", "longenough1")
    auth.set_user_active(conn, user.id, False, actor="admin")
    assert auth.authenticate(conn, "alex", "longenough1") is None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def test_session_round_trip(conn):
    auth.create_user(conn, username="alex", password="longenough1", role=auth.ROLE_REVIEWER, actor="admin")
    user = auth.authenticate(conn, "alex", "longenough1")
    token = auth.create_session(conn, user)

    resolved = auth.get_session_user(conn, token)
    assert resolved is not None
    assert resolved.username == "alex"

    auth.delete_session(conn, token)
    assert auth.get_session_user(conn, token) is None


def test_invalid_or_missing_session_token_resolves_to_none(conn):
    assert auth.get_session_user(conn, None) is None
    assert auth.get_session_user(conn, "not-a-real-token") is None


def test_expired_session_does_not_resolve(conn):
    auth.create_user(conn, username="alex", password="longenough1", role=auth.ROLE_REVIEWER, actor="admin")
    user = auth.authenticate(conn, "alex", "longenough1")
    token = auth.create_session(conn, user, ttl_hours=-1)  # already expired
    assert auth.get_session_user(conn, token) is None


def test_purge_expired_sessions(conn):
    auth.create_user(conn, username="alex", password="longenough1", role=auth.ROLE_REVIEWER, actor="admin")
    user = auth.authenticate(conn, "alex", "longenough1")
    auth.create_session(conn, user, ttl_hours=-1)
    auth.create_session(conn, user, ttl_hours=24)
    assert auth.purge_expired_sessions(conn) == 1
    remaining = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
    assert remaining == 1


# ---------------------------------------------------------------------------
# Permission matrix
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "role,permission,expected",
    [
        (auth.ROLE_PLATFORM_ADMIN, auth.PERM_MANAGE_USERS, True),
        (auth.ROLE_PLATFORM_ADMIN, auth.PERM_MANAGE_STATES, True),
        (auth.ROLE_STATE_ADMIN, auth.PERM_MANAGE_STATES, False),
        (auth.ROLE_STATE_ADMIN, auth.PERM_MANAGE_DISTRICTS, True),
        (auth.ROLE_STATE_ADMIN, auth.PERM_MANAGE_USERS, False),
        (auth.ROLE_REVIEWER, auth.PERM_REVIEW_RECORDS, True),
        (auth.ROLE_REVIEWER, auth.PERM_ESCALATE_RECORDS, True),
        (auth.ROLE_REVIEWER, auth.PERM_PUBLISH, False),
        (auth.ROLE_AUDITOR, auth.PERM_REVIEW_RECORDS, False),
        (auth.ROLE_READ_ONLY, auth.PERM_REVIEW_RECORDS, False),
    ],
)
def test_permission_matrix(role, permission, expected):
    user = auth.User(id=1, username="x", role=role, state_id=1 if role == auth.ROLE_STATE_ADMIN else None, active=True)
    assert user.has_permission(permission) is expected


def test_inactive_user_has_no_permissions():
    user = auth.User(id=1, username="x", role=auth.ROLE_PLATFORM_ADMIN, state_id=None, active=False)
    assert not user.has_permission(auth.PERM_MANAGE_USERS)


def test_state_admin_scoping():
    admin = auth.User(id=1, username="x", role=auth.ROLE_STATE_ADMIN, state_id=5, active=True)
    assert admin.can_act_on_state(5)
    assert not admin.can_act_on_state(6)
    assert not admin.can_act_on_state(None)

    platform_admin = auth.User(id=2, username="y", role=auth.ROLE_PLATFORM_ADMIN, state_id=None, active=True)
    assert platform_admin.can_act_on_state(5)
    assert platform_admin.can_act_on_state(999)


# ---------------------------------------------------------------------------
# HTTP: first-run setup, login, logout, and enforcement
# ---------------------------------------------------------------------------
@pytest.fixture
def app_client(settings):
    return TestClient(create_app(settings))


def test_first_run_redirects_every_protected_page_to_setup(app_client):
    # `/` is the public landing page (Phase 3.1) and never redirects; a
    # protected page is what proves first-run routing to /setup.
    assert app_client.get("/", follow_redirects=False).status_code == 200
    response = app_client.get("/workbench", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/setup")


def test_setup_creates_the_first_platform_admin(app_client, conn):
    response = app_client.post(
        "/setup",
        data={"username": "admin", "password": "adminpass123", "confirm_password": "adminpass123"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ops/dashboard"  # role-routed, not the public "/"

    user = auth.authenticate(conn, "admin", "adminpass123")
    assert user is not None
    assert user.role == auth.ROLE_PLATFORM_ADMIN

    # setup is a one-time step: once a user exists, it redirects to /login instead.
    again = app_client.get("/setup", follow_redirects=False)
    assert again.headers["location"] == "/login"


def test_setup_rejects_mismatched_passwords(app_client):
    response = app_client.post(
        "/setup", data={"username": "admin", "password": "adminpass123", "confirm_password": "different1"},
    )
    assert response.status_code == 400
    assert "do not match" in response.text


def test_public_landing_page_needs_no_auth(app_client, conn):
    auth.create_user(conn, username="admin", password="adminpass123", role=auth.ROLE_PLATFORM_ADMIN, actor="setup")
    assert app_client.get("/", follow_redirects=False).status_code == 200


def test_unauthenticated_request_to_a_protected_page_redirects_to_login(app_client, conn):
    auth.create_user(conn, username="admin", password="adminpass123", role=auth.ROLE_PLATFORM_ADMIN, actor="setup")
    response = app_client.get("/workbench", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/workbench"


def test_login_with_wrong_password_shows_error(app_client, conn):
    auth.create_user(conn, username="admin", password="adminpass123", role=auth.ROLE_PLATFORM_ADMIN, actor="setup")
    response = app_client.post("/login", data={"username": "admin", "password": "wrong"})
    assert response.status_code == 401
    assert "Invalid username or password" in response.text


def test_login_logout_round_trip(app_client, conn):
    login_as(app_client, conn)
    assert app_client.get("/workbench").status_code == 200

    app_client.post("/logout")
    response = app_client.get("/workbench", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


def test_reviewer_cannot_run_certification(conn, settings, parsed_documents):
    client = TestClient(create_app(settings))
    login_as(client, conn, username="reviewer1", role=auth.ROLE_REVIEWER)

    response = client.post("/certification/benchmark/run")
    assert response.status_code == 403
    assert "run_certification" in response.json()["detail"]


def test_reviewer_can_approve_records(conn, settings, parsed_documents):
    client = TestClient(create_app(settings))
    login_as(client, conn, username="reviewer1", role=auth.ROLE_REVIEWER)

    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    response = client.post(f"/records/{record_id}/approve", data={"note": "ok"}, follow_redirects=False)
    assert response.status_code == 303


def test_auditor_has_read_access_but_no_write_access(conn, settings, parsed_documents):
    client = TestClient(create_app(settings))
    login_as(client, conn, username="auditor1", role=auth.ROLE_AUDITOR)

    assert client.get("/workbench").status_code == 200
    assert client.get("/audit").status_code == 200

    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    response = client.post(f"/records/{record_id}/approve", data={"note": "x"})
    assert response.status_code == 403


def test_audit_trail_records_login_and_logout(conn, settings):
    client = TestClient(create_app(settings))
    login_as(client, conn)
    client.post("/logout")

    actions = [
        row["action"]
        for row in conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()
    ]
    assert "user.created" in actions
    assert "user.logged_in" in actions
    assert "user.logged_out" in actions
