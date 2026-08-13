"""Module 10 -- Audit & Governance Center UI (filterable view over audit_log)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from goengine.operations import geography
from goengine.workbench.app import create_app
from tests.conftest import login_as


@pytest.fixture
def client(conn, settings):
    test_client = TestClient(create_app(settings))
    login_as(test_client, conn)
    return test_client


def test_audit_page_lists_administrative_actions(client, conn):
    geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    response = client.get("/audit")
    assert response.status_code == 200
    assert "state.added" in response.text
    assert "user.created" in response.text  # from login_as's bootstrap user


def test_audit_page_filters_by_action(client, conn):
    # The action dropdown always lists every action that ever occurred (so a
    # user can switch filters from the current view) -- assert on the
    # results TABLE CELL specifically, not "appears anywhere on the page".
    geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    response = client.get("/audit?action=state.added")
    assert response.status_code == 200
    assert '<td class="mono">state.added</td>' in response.text
    assert '<td class="mono">user.created</td>' not in response.text


def test_audit_page_filters_by_entity_type(client, conn):
    geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    response = client.get("/audit?entity_type=state")
    assert response.status_code == 200
    assert '<td class="mono">state.added</td>' in response.text
    assert '<td class="mono">user.created</td>' not in response.text


def test_audit_page_action_dropdown_reflects_real_actions_only(client, conn):
    geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    response = client.get("/audit")
    # The action dropdown is built from actions that actually occurred --
    # something that never happened must not appear anywhere on the page.
    assert "district.published" not in response.text


def test_governance_actions_are_captured_end_to_end(client, conn):
    """A representative sweep of the blueprint's Module 10 audit events,
    each produced by its real code path, not asserted in isolation."""
    from goengine import review
    from goengine.operations import departments as ops_departments
    from goengine.operations import publication as ops_publication
    from goengine.operations import sources as ops_sources

    state_id = geography.add_state(conn, name="Tamil Nadu", code="TN", actor="admin")
    district_id = geography.add_district(conn, state_id=state_id, name="Chennai", code="CHE", actor="admin")
    source_id = ops_sources.create_source(
        conn, name="TN GO Portal", department="All", url="https://cms.tn.gov.in/x",
        source_type="go_portal", actor="admin",
    )
    ops_sources.edit_source(conn, source_id, department="Health", actor="admin", reason="correction")
    ops_sources.retire_source(conn, source_id, actor="admin", reason="decommissioned")

    response = client.get("/audit")
    actions_present = response.text
    for expected in (
        "state.added", "district.added", "source.registered", "source.edited", "source.retired",
    ):
        assert expected in actions_present, expected
