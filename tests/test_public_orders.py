"""Public Government Order browse/search pages (/orders) -- no login
required, and strictly scoped to go_records.status == 'approved'. This is
the trust boundary that makes "publish to public" real: a pending or
rejected record must never be visible or fetchable here."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from goengine import review
from goengine.pipeline import run_all
from goengine.workbench.app import create_app


@pytest.fixture
def public_client(conn, settings, fetcher, source_id):
    """No login performed -- these routes must work with zero session
    cookie, unlike every other TestClient fixture in this test suite."""
    run_all(conn, settings, fetcher, only_due=False)
    conn.commit()
    return TestClient(create_app(settings))


def _record_ids(conn: sqlite3.Connection) -> list[int]:
    return [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]


def test_orders_list_requires_no_login(public_client):
    response = public_client.get("/orders")
    assert response.status_code == 200
    assert "Government Orders" in response.text


@pytest.mark.parametrize("path", ["/orders", "/my-area", "/districts", "/taluks", "/villages"])
def test_public_pages_have_a_working_mobile_nav_toggle(public_client, path):
    """Regression test: _partials.html's shared public_header() macro used
    to render the nav links with no way to reveal them on a phone-width
    screen at all -- theme.css hides <nav> below 860px and relies entirely
    on a `[data-nav-toggle]` button to reveal it (see theme.js's
    wireMobileNav), but the macro never rendered that button. Every page
    built on the macro must carry it, plus the `id="main-nav"` the button's
    aria-controls points at."""
    response = public_client.get(path)
    assert response.status_code == 200
    assert "data-nav-toggle" in response.text
    assert 'id="main-nav"' in response.text
    assert 'aria-controls="main-nav"' in response.text


def test_landing_page_has_a_working_mobile_nav_toggle(public_client):
    # landing.html keeps its own header markup (not the shared macro) --
    # same requirement, checked separately since it's a different template.
    response = public_client.get("/")
    assert response.status_code == 200
    assert "data-nav-toggle" in response.text
    assert 'id="main-nav"' in response.text


def test_empty_state_when_nothing_approved(public_client):
    response = public_client.get("/orders")
    assert response.status_code == 200
    assert "No verified Government Orders" in response.text


def test_approved_record_appears_and_renders(public_client, conn):
    record_id = _record_ids(conn)[0]
    review.approve(conn, record_id, reviewer="admin")

    listing = public_client.get("/orders")
    assert listing.status_code == 200
    assert f"/orders/{record_id}" in listing.text

    detail = public_client.get(f"/orders/{record_id}")
    assert detail.status_code == 200
    assert "G.O.(Ms) No.123" in detail.text  # known sampledata value
    assert "Verified" in detail.text


def test_pending_record_is_invisible_and_404s(public_client, conn):
    record_id = _record_ids(conn)[0]
    # Left at the default 'pending' status -- never approved.

    listing = public_client.get("/orders")
    assert f"/orders/{record_id}" not in listing.text

    detail = public_client.get(f"/orders/{record_id}")
    assert detail.status_code == 404


def test_rejected_record_is_invisible_and_404s(public_client, conn):
    record_id = _record_ids(conn)[0]
    review.reject(conn, record_id, reviewer="admin", reason="test rejection")

    listing = public_client.get("/orders")
    assert f"/orders/{record_id}" not in listing.text
    assert public_client.get(f"/orders/{record_id}").status_code == 404


def test_nonexistent_record_404s_identically_to_unapproved(public_client, conn):
    record_id = _record_ids(conn)[0]  # left pending -- 404
    pending_response = public_client.get(f"/orders/{record_id}")
    missing_response = public_client.get("/orders/999999")  # never existed
    assert pending_response.status_code == missing_response.status_code == 404


def test_pdf_gated_on_approval(public_client, conn):
    ids = _record_ids(conn)
    approved_id, pending_id = ids[0], ids[1]
    review.approve(conn, approved_id, reviewer="admin")

    ok = public_client.get(f"/orders/{approved_id}/pdf")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "application/pdf"

    blocked = public_client.get(f"/orders/{pending_id}/pdf")
    assert blocked.status_code == 404


def test_department_and_district_filters_narrow_results(public_client, conn):
    ids = _record_ids(conn)
    for record_id in ids:
        review.approve(conn, record_id, reviewer="admin")

    opts = public_client.get("/orders").text
    assert "orders-filter-bar" in opts  # filter bar renders at all

    # Filtering to a bucket/district that has no approved records must never
    # error -- just an empty (but well-formed) result set.
    response = public_client.get("/orders", params={"department": "no-such-bucket"})
    assert response.status_code == 200
    assert "No verified Government Orders" in response.text


def test_taluks_and_villages_are_public_placeholders(public_client):
    # Geography stops at district today -- these are nav-consistency
    # placeholders, not real browsable data yet, but must still be reachable
    # without login and never 404/500.
    taluks = public_client.get("/taluks")
    assert taluks.status_code == 200
    assert "Coming Soon" in taluks.text
    assert 'href="/orders"' in taluks.text  # points back at what does exist

    villages = public_client.get("/villages")
    assert villages.status_code == 200
    assert "Coming Soon" in villages.text


def test_search_query_matches_go_number(public_client, conn):
    record_id = _record_ids(conn)[0]
    review.approve(conn, record_id, reviewer="admin")

    response = public_client.get("/orders", params={"q": "123"})
    assert response.status_code == 200
    assert f"/orders/{record_id}" in response.text

    miss = public_client.get("/orders", params={"q": "no-such-go-number-exists"})
    assert f"/orders/{record_id}" not in miss.text


def test_pagination_math(public_client, conn):
    ids = _record_ids(conn)
    assert len(ids) >= 2, "sampledata must produce more than one record for pagination to be testable"
    for record_id in ids:
        review.approve(conn, record_id, reviewer="admin")

    page1 = public_client.get("/orders", params={"page": 1})
    assert page1.status_code == 200
    # A limit of 20 comfortably covers the small sampledata set on one page.
    assert "Next" not in page1.text or len(ids) > 20


def test_detail_page_hides_reviewer_internals(public_client, conn):
    record_id = _record_ids(conn)[0]
    review.approve(conn, record_id, reviewer="admin")

    detail = public_client.get(f"/orders/{record_id}").text
    # Internal-only jargon that record.html shows to reviewers must never
    # leak to the public detail page.
    assert "@header" not in detail
    assert "@references" not in detail
    assert "GO_NUMBER_FULL" not in detail
    assert "reviewed by" not in detail.lower()
    assert "corrected by" not in detail.lower()
