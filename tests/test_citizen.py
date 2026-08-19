"""Phase 4A -- citizen accounts: registration, login, saved searches,
bookmarks, and download gating.

The core property under test is that citizen_users/citizen_sessions are a
genuinely separate identity system from staff users/sessions (see
schema_citizen.sql's header comment for why): a citizen session must never
satisfy a staff-only LoggedIn route, and a staff session must never satisfy
RequireCitizen. That's what makes it safe to ship self-registration without
retrofitting every existing admin route.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from goengine import review
from goengine.operations import citizen as ops_citizen
from goengine.pipeline import run_all
from goengine.workbench.app import create_app
from tests.conftest import login_as


@pytest.fixture
def client(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    conn.commit()
    return TestClient(create_app(settings))


def _record_ids(conn: sqlite3.Connection) -> list[int]:
    return [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]


def _register(client, *, email="citizen@example.com", password="password123", full_name="Test Citizen", **extra):
    data = {
        "full_name": full_name, "email": email, "mobile": "9876543210",
        "password": password, "confirm_password": password, "terms_accepted": "1",
    }
    data.update(extra)
    return client.post("/register", data=data, follow_redirects=False)


def _citizen_login(client, *, email="citizen@example.com", password="password123", **extra):
    data = {"email": email, "password": password}
    data.update(extra)
    return client.post("/citizen/login", data=data, follow_redirects=False)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def test_registration_creates_session_and_redirects_to_dashboard(client):
    response = _register(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    assert "thirdeye_citizen_session" in client.cookies

    dash = client.get("/dashboard")
    assert dash.status_code == 200
    assert "Test Citizen" in dash.text


def test_registration_rejects_duplicate_email(client):
    _register(client, email="dup@example.com")
    client.cookies.clear()
    response = _register(client, email="dup@example.com")
    assert response.status_code == 400
    assert "already exists" in response.text


def test_registration_rejects_short_password(client):
    response = _register(client, password="short", confirm_password="short")
    assert response.status_code == 400


def test_registration_requires_password_confirmation_match(client):
    response = _register(client, password="password123", confirm_password="different123")
    assert response.status_code == 400
    assert "do not match" in response.text.lower()


def test_registration_requires_terms_acceptance(client):
    response = _register(client, terms_accepted="")
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------
def test_login_round_trip(client):
    _register(client, email="login@example.com")
    client.cookies.clear()

    response = _citizen_login(client, email="login@example.com")
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"

    logout = client.post("/citizen/logout", follow_redirects=False)
    assert logout.status_code == 303
    assert "thirdeye_citizen_session" not in client.cookies

    assert client.get("/dashboard", follow_redirects=False).status_code == 303


def test_login_rejects_wrong_password(client):
    _register(client, email="wrongpw@example.com")
    client.cookies.clear()
    response = _citizen_login(client, email="wrongpw@example.com", password="wrongpassword")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Cross-contamination: the whole reason for a separate cookie/table
# ---------------------------------------------------------------------------
def test_citizen_session_does_not_satisfy_staff_routes(client, conn):
    from goengine.operations import auth as ops_auth

    # A staff account must already exist, otherwise require_login()'s
    # first-run branch sends anyone unauthenticated to /setup instead of
    # /login -- unrelated to the citizen-vs-staff boundary this test checks.
    ops_auth.create_user(conn, username="admin", password="testpass123", role=ops_auth.ROLE_PLATFORM_ADMIN, actor="test")

    _register(client)
    # /workbench requires staff LoggedIn -- a citizen session must not pass.
    response = client.get("/workbench", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")

    record_id = _record_ids(conn)[0]
    response = client.get(f"/records/{record_id}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_staff_session_does_not_satisfy_citizen_routes(client, conn):
    login_as(client, conn)  # platform_admin staff session
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/citizen/login")


# ---------------------------------------------------------------------------
# Download gating
# ---------------------------------------------------------------------------
def test_anonymous_download_redirects_to_citizen_login(client, conn):
    record_id = _record_ids(conn)[0]
    review.approve(conn, record_id, reviewer="admin")

    for fmt in ("pdf", "text", "metadata"):
        response = client.get(f"/orders/{record_id}/download/{fmt}", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"/citizen/login?next=/orders/{record_id}/download/{fmt}"


def test_citizen_can_download_all_three_formats_and_it_is_logged(client, conn):
    record_id = _record_ids(conn)[0]
    review.approve(conn, record_id, reviewer="admin")
    _register(client, email="downloader@example.com")

    pdf = client.get(f"/orders/{record_id}/download/pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["content-disposition"].startswith("attachment")

    text = client.get(f"/orders/{record_id}/download/text")
    assert text.status_code == 200
    assert "G.O.(Ms) No.123" in text.text or len(text.text) > 0

    metadata = client.get(f"/orders/{record_id}/download/metadata")
    assert metadata.status_code == 200
    assert metadata.headers["content-type"].startswith("application/json")
    body = metadata.json()
    assert body["record_id"] == record_id

    citizen_id = conn.execute("SELECT id FROM citizen_users WHERE email='downloader@example.com'").fetchone()["id"]
    logged = conn.execute(
        "SELECT format FROM download_log WHERE citizen_id = ? ORDER BY format", (citizen_id,)
    ).fetchall()
    assert sorted(r["format"] for r in logged) == ["metadata", "pdf", "text"]


def test_staff_can_also_download(client, conn):
    """The blueprint says 'authenticated users' can download, not 'citizens
    only' -- a logged-in staff member must succeed too, logged under
    staff_user_id rather than citizen_id."""
    record_id = _record_ids(conn)[0]
    review.approve(conn, record_id, reviewer="admin")
    login_as(client, conn)

    response = client.get(f"/orders/{record_id}/download/pdf")
    assert response.status_code == 200

    row = conn.execute("SELECT citizen_id, staff_user_id FROM download_log WHERE format='pdf'").fetchone()
    assert row["citizen_id"] is None
    assert row["staff_user_id"] is not None


def test_download_404s_for_unapproved_record_even_when_logged_in(client, conn):
    record_id = _record_ids(conn)[0]  # left pending
    _register(client)
    response = client.get(f"/orders/{record_id}/download/pdf")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Saved records (bookmarks)
# ---------------------------------------------------------------------------
def test_bookmark_toggle(client, conn):
    record_id = _record_ids(conn)[0]
    review.approve(conn, record_id, reviewer="admin")
    _register(client, email="bookmarker@example.com")

    dash_before = client.get("/dashboard").text
    assert "No saved records yet" in dash_before

    client.post(f"/orders/{record_id}/save", follow_redirects=False)
    dash_after = client.get("/dashboard").text
    assert "No saved records yet" not in dash_after
    assert "G.O.(Ms) No.123" in dash_after

    client.post(f"/orders/{record_id}/save", follow_redirects=False)  # toggle off
    dash_final = client.get("/dashboard").text
    assert "No saved records yet" in dash_final


def test_saved_records_are_scoped_per_citizen(client, conn):
    record_id = _record_ids(conn)[0]
    review.approve(conn, record_id, reviewer="admin")

    _register(client, email="alice@example.com")
    client.post(f"/orders/{record_id}/save")
    alice_id = conn.execute("SELECT id FROM citizen_users WHERE email='alice@example.com'").fetchone()["id"]

    client.cookies.clear()
    _register(client, email="bob@example.com")
    bob_id = conn.execute("SELECT id FROM citizen_users WHERE email='bob@example.com'").fetchone()["id"]

    assert len(ops_citizen.list_saved_records(conn, alice_id)) == 1
    assert len(ops_citizen.list_saved_records(conn, bob_id)) == 0


# ---------------------------------------------------------------------------
# Saved searches
# ---------------------------------------------------------------------------
def test_save_and_delete_search(client, conn):
    _register(client, email="searcher@example.com")

    response = client.post(
        "/dashboard/saved-searches",
        data={"label": "Health GOs", "department": "health", "district": "", "q": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303

    dash = client.get("/dashboard").text
    assert "Health GOs" in dash

    citizen_id = conn.execute("SELECT id FROM citizen_users WHERE email='searcher@example.com'").fetchone()["id"]
    search_id = ops_citizen.list_saved_searches(conn, citizen_id)[0]["id"]

    client.post(f"/dashboard/saved-searches/{search_id}/delete", follow_redirects=False)
    dash_after = client.get("/dashboard").text
    assert "Health GOs" not in dash_after
    assert "No saved searches yet" in dash_after
