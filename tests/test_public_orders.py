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
    slug = conn.execute("SELECT go_url_slug FROM go_records WHERE id = ?", (record_id,)).fetchone()["go_url_slug"]

    listing = public_client.get("/orders")
    assert listing.status_code == 200
    assert f"/go/{slug}" in listing.text  # the permanent URL, preferred when available

    # /orders/{id} keeps working unchanged -- old links/bookmarks must never break.
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
    assert "GO123/2026" in response.text  # known sampledata identity

    miss = public_client.get("/orders", params={"q": "no-such-go-number-exists"})
    assert "GO123/2026" not in miss.text


def test_search_query_matches_the_new_canonical_identifier_format(public_client, conn):
    # sampledata's first sample is "G.O.(Ms) No.123" dated 2026-03-15.
    record_id = _record_ids(conn)[0]
    review.approve(conn, record_id, reviewer="admin")

    for query in ("GO123/2026", "go123/2026", "GO123"):
        response = public_client.get("/orders", params={"q": query})
        assert response.status_code == 200, query
        assert "GO123/2026" in response.text, query

    wrong_year = public_client.get("/orders", params={"q": "GO123/2020"})
    assert "GO123/2026" not in wrong_year.text


def test_orders_list_and_detail_show_the_citizen_facing_identifier(public_client, conn):
    record_id = _record_ids(conn)[0]
    review.approve(conn, record_id, reviewer="admin")

    listing = public_client.get("/orders")
    assert "GO123/2026" in listing.text

    detail = public_client.get(f"/orders/{record_id}")
    assert "GO123/2026" in detail.text


def test_permanent_go_url_renders_the_same_record_as_the_id_based_url(public_client, conn):
    record_id = _record_ids(conn)[0]
    review.approve(conn, record_id, reviewer="admin")
    slug = conn.execute("SELECT go_url_slug FROM go_records WHERE id = ?", (record_id,)).fetchone()["go_url_slug"]
    assert slug is not None

    by_slug = public_client.get(f"/go/{slug}")
    by_id = public_client.get(f"/orders/{record_id}")
    assert by_slug.status_code == by_id.status_code == 200
    assert "GO123/2026" in by_slug.text
    assert f'<link rel="canonical" href="/go/{slug}">' in by_slug.text
    assert f'<link rel="canonical" href="/go/{slug}">' in by_id.text


def test_permanent_go_url_404s_for_an_unapproved_or_nonexistent_slug(public_client, conn):
    record_id = _record_ids(conn)[0]  # left pending -- not approved
    slug = conn.execute("SELECT go_url_slug FROM go_records WHERE id = ?", (record_id,)).fetchone()["go_url_slug"]

    assert public_client.get(f"/go/{slug}").status_code == 404
    assert public_client.get("/go/no-such-slug").status_code == 404


def test_orders_list_links_to_the_permanent_url_when_available(public_client, conn):
    record_id = _record_ids(conn)[0]
    review.approve(conn, record_id, reviewer="admin")
    slug = conn.execute("SELECT go_url_slug FROM go_records WHERE id = ?", (record_id,)).fetchone()["go_url_slug"]

    listing = public_client.get("/orders")
    assert f'href="/go/{slug}"' in listing.text
    assert f'href="/orders/{record_id}"' not in listing.text


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


# ---------------------------------------------------------------------------
# Phase 3.5 Initiative 8 -- Search Excellence
# ---------------------------------------------------------------------------
def _approved_record_under(conn: sqlite3.Connection, *, department: str, go_number: str, go_date: str) -> int:
    """Builds one approved go_records row under a source with the given
    department -- the sample fixtures all share one source ("All
    Departments"), which maps to a generic code, so the department-code
    search tests need a record under a REAL department to be meaningful."""
    from goengine import go_identity, registry, review
    from goengine.db import utcnow

    now = utcnow()
    source = registry.add_source(
        conn, name=f"{department} test source {now}", department=department,
        url=f"https://www.tn.gov.in/go.php?dep_id={department}", source_type="department_site",
    )
    discovered_id = conn.execute(
        "INSERT INTO discovered_documents (source_id, url, discovered_at, last_seen_at) VALUES (?, ?, ?, ?)",
        (source, f"https://tn.gov.in/doc/{now}", now, now),
    ).lastrowid
    document_id = conn.execute(
        """
        INSERT INTO documents
            (discovered_id, source_id, source_url, file_name, stored_path, sha256, byte_size, downloaded_at)
        VALUES (?, ?, ?, 'go.pdf', 'go.pdf', ?, 100, ?)
        """,
        (discovered_id, source, f"https://tn.gov.in/doc/{now}", f"sha-{now}", now),
    ).lastrowid
    extraction_id = conn.execute(
        "INSERT INTO extractions (document_id, backend, page_count, char_count, confidence, extracted_at) "
        "VALUES (?, 'pymupdf', 1, 100, 0.9, ?)",
        (document_id, now),
    ).lastrowid
    record_id = conn.execute(
        "INSERT INTO go_records (extraction_id, document_id, source_id, extractor_version, created_at) "
        "VALUES (?, ?, ?, 'test-1.0', ?)",
        (extraction_id, document_id, source, now),
    ).lastrowid
    for field_name, value in (("go_number", go_number), ("go_date", go_date), ("department", department), ("subject", "Test subject")):
        conn.execute(
            "INSERT INTO go_fields (record_id, field_name, value, normalized_value, source_page, source_text, confidence, method, created_at) "
            "VALUES (?, ?, ?, ?, 1, 'evidence', 0.9, 'test', ?)",
            (record_id, field_name, value, value, now),
        )
    go_identity.compute_identity(conn, int(record_id))
    review.approve(conn, int(record_id), reviewer="admin")
    return int(record_id)


def test_search_by_department_code_and_go_number(conn, settings):
    from goengine import public

    record_id = _approved_record_under(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-06-01",
    )

    records, total = public.search(conn, q="HFW GO41")
    assert total == 1
    assert records[0].record_id == record_id

    # Wrong department code must not match.
    _, total_wrong = public.search(conn, q="PWD GO41")
    assert total_wrong == 0


def test_search_by_department_code_go_number_and_year(conn, settings):
    from goengine import public

    record_id = _approved_record_under(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-06-01",
    )

    records, total = public.search(conn, q="HFW GO41/2022")
    assert total == 1
    assert records[0].record_id == record_id

    _, total_wrong_year = public.search(conn, q="HFW GO41/2021")
    assert total_wrong_year == 0


def test_search_by_bare_department_name(conn, settings):
    from goengine import public

    record_id = _approved_record_under(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-06-01",
    )

    records, total = public.search(conn, q="Health and Family Welfare")
    assert total == 1
    assert records[0].record_id == record_id

    # Case-insensitive.
    records_lower, total_lower = public.search(conn, q="health and family welfare")
    assert total_lower == 1
    assert records_lower[0].record_id == record_id


def test_search_by_bare_year(conn, settings):
    from goengine import public

    matching = _approved_record_under(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-06-01",
    )
    _approved_record_under(
        conn, department="School Education", go_number="G.O.(Ms) No.7", go_date="2021-01-01",
    )

    records, total = public.search(conn, q="2022")
    assert total == 1
    assert records[0].record_id == matching


def test_existing_search_patterns_still_work_alongside_the_new_ones(conn, settings):
    from goengine import public

    record_id = _approved_record_under(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-06-01",
    )

    _, total_bare = public.search(conn, q="GO41")
    assert total_bare == 1
    _, total_with_year = public.search(conn, q="GO41/2022")
    assert total_with_year == 1
    _, total_free_text = public.search(conn, q="Test subject")
    assert total_free_text == 1
    assert record_id > 0
    assert public.search(conn, q="no-such-go-anywhere")[1] == 0
