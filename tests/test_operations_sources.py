"""Modules 4 & 5 -- Versioned Government Source Registry + Certification Center."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from goengine import registry
from goengine.operations import sources as ops_sources
from goengine.workbench.app import create_app, get_fetcher
from tests.conftest import login_as


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------
def test_create_source_writes_version_one(conn):
    source_id = ops_sources.create_source(
        conn, name="TN GO Portal", department="All", url="https://cms.tn.gov.in/go-search",
        source_type="go_portal", discovery_method="listing_page", actor="admin",
    )
    history = ops_sources.version_history(conn, source_id)
    assert len(history) == 1
    assert history[0]["version"] == 1
    assert history[0]["url"] == "https://cms.tn.gov.in/go-search"

    row = conn.execute("SELECT current_version, lifecycle_status FROM sources WHERE id = ?", (source_id,)).fetchone()
    assert row["current_version"] == 1
    assert row["lifecycle_status"] == "NEW"


def test_edit_creates_a_new_version_never_overwrites(conn):
    source_id = ops_sources.create_source(
        conn, name="X", department="D", url="https://cms.tn.gov.in/old", source_type="go_portal", actor="admin",
    )
    ops_sources.edit_source(conn, source_id, url="https://cms.tn.gov.in/new", actor="admin", reason="moved")

    history = ops_sources.version_history(conn, source_id)
    assert [h["version"] for h in history] == [1, 2]
    assert history[0]["url"] == "https://cms.tn.gov.in/old"  # original preserved
    assert history[1]["url"] == "https://cms.tn.gov.in/new"

    current = conn.execute("SELECT url, current_version FROM sources WHERE id = ?", (source_id,)).fetchone()
    assert current["url"] == "https://cms.tn.gov.in/new"
    assert current["current_version"] == 2


def test_edit_requires_a_reason(conn):
    source_id = ops_sources.create_source(
        conn, name="X", department="D", url="https://cms.tn.gov.in/x", source_type="go_portal", actor="admin",
    )
    with pytest.raises(ops_sources.SourceOperationsError, match="reason"):
        ops_sources.edit_source(conn, source_id, name="Y", actor="admin", reason="")


def test_edit_rejects_url_outside_the_allowlist(conn):
    source_id = ops_sources.create_source(
        conn, name="X", department="D", url="https://cms.tn.gov.in/x", source_type="go_portal", actor="admin",
    )
    with pytest.raises(ops_sources.SourceOperationsError):
        ops_sources.edit_source(conn, source_id, url="https://evil.example.com/x", actor="admin", reason="oops")
    # Unaffected by the rejected attempt.
    assert conn.execute("SELECT url FROM sources WHERE id = ?", (source_id,)).fetchone()["url"] == "https://cms.tn.gov.in/x"


def test_source_versions_table_is_append_only(conn):
    source_id = ops_sources.create_source(
        conn, name="X", department="D", url="https://cms.tn.gov.in/x", source_type="go_portal", actor="admin",
    )
    version_id = ops_sources.version_history(conn, source_id)[0]["id"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE source_versions SET url = 'https://cms.tn.gov.in/tampered' WHERE id = ?", (version_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM source_versions WHERE id = ?", (version_id,))


def test_retire_disables_and_sets_terminal_lifecycle(conn):
    source_id = ops_sources.create_source(
        conn, name="X", department="D", url="https://cms.tn.gov.in/x", source_type="go_portal", actor="admin",
    )
    ops_sources.retire_source(conn, source_id, actor="admin", reason="decommissioned")
    row = conn.execute("SELECT lifecycle_status, active FROM sources WHERE id = ?", (source_id,)).fetchone()
    assert row["lifecycle_status"] == "RETIRED"
    assert row["active"] == 0


def test_clone_creates_an_independent_source_with_its_own_history(conn):
    source_id = ops_sources.create_source(
        conn, name="Original", department="D", url="https://cms.tn.gov.in/x", source_type="go_portal", actor="admin",
    )
    clone_id = ops_sources.clone_source(conn, source_id, actor="admin")
    assert clone_id != source_id

    clone_row = conn.execute("SELECT name, lifecycle_status FROM sources WHERE id = ?", (clone_id,)).fetchone()
    assert clone_row["name"] == "Original (clone)"
    assert clone_row["lifecycle_status"] == "NEW"
    assert len(ops_sources.version_history(conn, clone_id)) == 1

    # Editing the clone must not touch the original's history.
    ops_sources.edit_source(conn, clone_id, url="https://cms.tn.gov.in/y", actor="admin", reason="tweak")
    assert len(ops_sources.version_history(conn, source_id)) == 1
    assert len(ops_sources.version_history(conn, clone_id)) == 2


def test_clone_names_avoid_collision(conn):
    source_id = ops_sources.create_source(
        conn, name="X", department="D", url="https://cms.tn.gov.in/x", source_type="go_portal", actor="admin",
    )
    ops_sources.clone_source(conn, source_id, actor="admin")
    second_clone = ops_sources.clone_source(conn, source_id, actor="admin")
    name = conn.execute("SELECT name FROM sources WHERE id = ?", (second_clone,)).fetchone()["name"]
    assert name == "X (clone 2)"


def test_quick_test_advances_lifecycle_to_tested(conn, fetcher, source_id):
    from goengine.operations.sources import quick_test_source

    ok, message = quick_test_source(conn, fetcher, source_id, actor="admin")
    assert ok
    assert conn.execute("SELECT lifecycle_status FROM sources WHERE id = ?", (source_id,)).fetchone()["lifecycle_status"] == "TESTED"


def test_quick_test_failure_does_not_advance_lifecycle(conn):
    from goengine.fetching import OfflineFetcher
    from goengine.operations.sources import quick_test_source

    source_id = ops_sources.create_source(
        conn, name="X", department="D", url="https://cms.tn.gov.in/unreachable", source_type="go_portal", actor="admin",
    )
    ok, message = quick_test_source(conn, OfflineFetcher(), source_id, actor="admin")
    assert not ok
    assert conn.execute("SELECT lifecycle_status FROM sources WHERE id = ?", (source_id,)).fetchone()["lifecycle_status"] == "NEW"


def test_advance_lifecycle_on_certification_only_on_certified_result(conn):
    source_id = ops_sources.create_source(
        conn, name="X", department="D", url="https://cms.tn.gov.in/x", source_type="go_portal", actor="admin",
    )
    ops_sources.advance_lifecycle_on_certification(conn, source_id, "FAILED")
    assert conn.execute("SELECT lifecycle_status FROM sources WHERE id = ?", (source_id,)).fetchone()["lifecycle_status"] == "NEW"

    ops_sources.advance_lifecycle_on_certification(conn, source_id, "CERTIFIED")
    assert conn.execute("SELECT lifecycle_status FROM sources WHERE id = ?", (source_id,)).fetchone()["lifecycle_status"] == "CERTIFIED"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
@pytest.fixture
def client(conn, settings, fetcher):
    app = create_app(settings)
    app.dependency_overrides[get_fetcher] = lambda: fetcher
    test_client = TestClient(app)
    login_as(test_client, conn)
    return test_client


def test_add_edit_via_http(client):
    add = client.post(
        "/ops/sources/add",
        data={"name": "TN GO Portal", "department": "All", "url": "https://cms.tn.gov.in/go-search", "source_type": "go_portal"},
        follow_redirects=False,
    )
    assert add.status_code == 303
    source_id = add.headers["location"].rsplit("/", 1)[-1]

    detail = client.get(f"/ops/sources/{source_id}")
    assert "v1" in detail.text

    edit = client.post(
        f"/ops/sources/{source_id}/edit", data={"department": "Health", "reason": "corrected department"},
    )
    assert edit.status_code == 200
    assert "Health" in client.get(f"/ops/sources/{source_id}").text


def test_non_admin_cannot_add_source(conn, settings, fetcher):
    app = create_app(settings)
    app.dependency_overrides[get_fetcher] = lambda: fetcher
    client = TestClient(app)
    login_as(client, conn, username="reviewer1", role="reviewer")

    response = client.post(
        "/ops/sources/add",
        data={"name": "X", "department": "D", "url": "https://cms.tn.gov.in/x", "source_type": "go_portal"},
    )
    assert response.status_code == 403


def test_full_certification_from_source_detail_page_advances_lifecycle(client):
    add = client.post(
        "/ops/sources/add",
        data={"name": "TN GO Portal", "department": "All", "url": "https://cms.tn.gov.in/go-search", "source_type": "go_portal"},
        follow_redirects=False,
    )
    source_id = add.headers["location"].rsplit("/", 1)[-1]

    response = client.post(f"/certification/sources/{source_id}/certify", follow_redirects=False)
    assert response.status_code == 303

    detail = client.get(f"/ops/sources/{source_id}")
    assert "CERTIFIED" in detail.text
