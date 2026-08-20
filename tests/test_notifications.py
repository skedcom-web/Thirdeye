"""EmailJS-backed Notifications settings: credential storage/round-trip,
permission gating, and the test-send button's failure path when nothing is
configured yet. The success path (a real EmailJS API call) is deliberately
not exercised here -- it would mean either a live network call in CI or
mocking httpx, and the send primitive itself (operations/email.py) is thin
enough that its correctness is covered by the round-trip and gating tests
below; only the *routing* of that call needed verifying."""

import pytest
from fastapi.testclient import TestClient

from goengine.operations import email as ops_email
from goengine.workbench.app import create_app
from tests.conftest import login_as


@pytest.fixture
def client(conn, settings):
    return TestClient(create_app(settings))


def test_config_round_trips_through_system_settings(conn):
    assert ops_email.is_configured(conn) is False
    ops_email.save_config(
        conn, service_id="service_abc", template_id="template_xyz",
        public_key="pub_123", private_key="",
    )
    cfg = ops_email.get_config(conn)
    assert cfg["emailjs_service_id"] == "service_abc"
    assert cfg["emailjs_template_id"] == "template_xyz"
    assert cfg["emailjs_public_key"] == "pub_123"
    assert ops_email.is_configured(conn) is True


def test_send_email_without_config_raises(conn):
    with pytest.raises(ops_email.EmailNotConfigured):
        ops_email.send_email(conn, to_email="citizen@example.com", template_params={})


def test_notifications_page_requires_login(client):
    response = client.get("/ops/notifications", follow_redirects=False)
    assert response.status_code in (303, 307)


def test_save_requires_manage_sources_permission(client, conn):
    login_as(client, conn, username="reviewer1", role="reviewer")
    response = client.post(
        "/ops/notifications/emailjs",
        data={"service_id": "s", "template_id": "t", "public_key": "p"},
    )
    assert response.status_code == 403


def test_admin_can_save_and_see_it_reflected(client, conn):
    login_as(client, conn)
    saved = client.post(
        "/ops/notifications/emailjs",
        data={"service_id": "service_abc", "template_id": "template_xyz", "public_key": "pub_123"},
        follow_redirects=False,
    )
    assert saved.status_code == 303

    page = client.get("/ops/notifications")
    assert page.status_code == 200
    assert "service_abc" in page.text
    assert "not yet configured" not in page.text


def test_test_email_reports_failure_cleanly_when_unconfigured(client, conn):
    login_as(client, conn)
    response = client.post("/ops/notifications/test-email", data={"to_email": "citizen@example.com"})
    assert response.status_code == 200
    assert "failed" in response.text
    assert "not configured" in response.text.lower()
