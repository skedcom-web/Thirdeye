"""Module 7 HTTP surface, and the golden dataset harness."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from goengine import benchmark, review
from goengine.pipeline import run_all
from goengine.sampledata import write_samples
from goengine.workbench.app import create_app
from tests.conftest import login_as


@pytest.fixture
def client(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    conn.commit()
    test_client = TestClient(create_app(settings))
    login_as(test_client, conn)
    return test_client


def test_dashboard_lists_the_queue(client):
    # `/` is Phase 3.1's public landing page; the Phase 1 workbench dashboard
    # moved to /workbench so it can stay behind login.
    response = client.get("/workbench")
    assert response.status_code == 200
    assert "Verification queue" in response.text


def test_record_page_shows_evidence(client, conn):
    record_id = int(conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"])
    response = client.get(f"/records/{record_id}")

    assert response.status_code == 200
    assert "G.O.(Ms) No.123" in response.text
    assert "Extracted metadata" in response.text
    assert "Provenance" in response.text


def test_original_pdf_is_served_unchanged(client, conn, settings):
    from goengine import repository

    row = conn.execute("SELECT id, stored_path FROM documents LIMIT 1").fetchone()
    response = client.get(f"/documents/{row['id']}/pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    # Must render in the review iframe rather than download.
    assert response.headers["content-disposition"].startswith("inline")
    assert response.content == repository.absolute_path(settings, row["stored_path"]).read_bytes()


def test_integrity_endpoint(client, conn):
    document_id = int(conn.execute("SELECT MIN(id) AS id FROM documents").fetchone()["id"])
    response = client.get(f"/documents/{document_id}/verify")
    assert response.json()["ok"] is True


def test_record_page_verify_button_calls_api_not_a_plain_link(client, conn):
    """Regression test: 'verify integrity' used to be a plain <a href> to a
    JSON API route, so clicking it navigated the whole browser to a raw
    {"ok": true, ...} page with no way back. It must now be a button that
    stays on the record page and calls the API via JS instead."""
    record_id = int(conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"])
    document_id = int(conn.execute(
        "SELECT document_id FROM go_records WHERE id = ?", (record_id,)
    ).fetchone()["document_id"])
    response = client.get(f"/records/{record_id}")

    assert response.status_code == 200
    assert f'href="/documents/{document_id}/verify"' not in response.text
    assert f'data-verify-url="/documents/{document_id}/verify"' in response.text
    assert 'id="verify-integrity-btn"' in response.text


def test_documents_list_page_out_of_range_is_clamped_not_an_error(client, conn):
    response = client.get("/ops/documents?page=999")
    assert response.status_code == 200
    assert "Downloaded documents" in response.text


def test_review_hub_page_out_of_range_is_clamped_not_an_error(client, conn):
    response = client.get("/ops/review?queue=extraction&page=999")
    assert response.status_code == 200
    assert "Pending Review Queue" in response.text


def test_approve_via_http_publishes(client, conn):
    record_id = int(conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"])
    response = client.post(
        f"/records/{record_id}/approve",
        data={"note": "ok"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    published = client.get("/api/verified").json()
    assert [r["record_id"] for r in published] == [record_id]


def test_correct_via_http(client, conn):
    record_id = int(conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"])
    response = client.post(
        f"/records/{record_id}/correct",
        data={"field_name": "department", "new_value": "Finance", "source_page": 1},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get(f"/records/{record_id}").text.count("Finance") >= 1


def test_reject_requires_a_reason_over_http(client, conn):
    """A wholly empty form field is rejected by FastAPI's own Form()
    validation before the route ever runs (a framework quirk, not this
    route's doing) -- that stays a plain 422."""
    record_id = int(conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"])
    response = client.post(f"/records/{record_id}/reject", data={"reason": ""}, follow_redirects=False)

    assert response.status_code == 422
    assert review.get_summary(conn, record_id).status == "pending"


def test_reject_with_whitespace_only_reason_shows_friendly_error(client, conn):
    """A reason that's present but blank after stripping is exactly what
    review.reject() itself refuses -- this exercises that path (as opposed
    to FastAPI's own Form() validation above) and confirms it now sends the
    reviewer back to the record page with a readable banner instead of a
    raw JSON error (see _record_error_redirect)."""
    record_id = int(conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"])
    response = client.post(f"/records/{record_id}/reject", data={"reason": "   "}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/records/{record_id}?error=")
    assert review.get_summary(conn, record_id).status == "pending"


def test_approve_with_missing_core_field_shows_friendly_error(client, conn):
    """Regression test for the raw-JSON-error bug: approving a record still
    missing a core field must land back on the record page with a readable
    banner, not FastAPI's default JSON error rendering."""
    record_id = int(conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"])
    conn.execute("DELETE FROM go_fields WHERE record_id = ? AND field_name = 'go_number'", (record_id,))

    response = client.post(f"/records/{record_id}/approve", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/records/{record_id}?error=cannot%20approve%20with%20missing%20core%20fields%3A%20go_number%20--%20correct%20them%20first%2C%20or%20approve%20with%20the%20override"
    assert review.get_summary(conn, record_id).status == "pending"

    followed = client.get(response.headers["location"])
    assert followed.status_code == 200
    assert "could not save" in followed.text
    assert "cannot approve with missing core fields: go_number" in followed.text


def test_bulk_approve_via_http_approves_all_selected(client, conn):
    record_ids = [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]
    assert len(record_ids) >= 2  # the sample fixture ships 3 GOs

    response = client.post(
        "/records/bulk-approve",
        data={"record_ids": record_ids, "queue": "extraction"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "bulk_approved=" + str(len(record_ids)) in response.headers["location"]

    for record_id in record_ids:
        assert review.get_summary(conn, record_id).status == "approved"

    published_ids = {r["record_id"] for r in client.get("/api/verified").json()}
    assert published_ids == set(record_ids)


def test_bulk_approve_skips_record_with_missing_core_field(client, conn):
    ids = [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]
    good_id, broken_id = ids[0], ids[1]

    # Simulate a record with no go_number left standing -- exactly what
    # review.approve() refuses without an explicit override.
    conn.execute(
        "DELETE FROM go_fields WHERE record_id = ? AND field_name = 'go_number'",
        (broken_id,),
    )

    response = client.post(
        "/records/bulk-approve",
        data={"record_ids": [good_id, broken_id], "queue": "extraction"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "bulk_approved=1" in response.headers["location"]
    assert "bulk_skipped=1" in response.headers["location"]

    assert review.get_summary(conn, good_id).status == "approved"
    assert review.get_summary(conn, broken_id).status == "pending"


def test_bulk_approve_with_no_selection_is_a_noop(client, conn):
    response = client.post(
        "/records/bulk-approve", data={"queue": "extraction"}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert "bulk_approved=0" in response.headers["location"]


def test_missing_record_is_404(client):
    assert client.get("/records/9999").status_code == 404


def test_audit_api(client):
    entries = client.get("/api/audit").json()
    assert any(e["action"] == "document.discovered" for e in entries)


# ---------------------------------------------------------------------------
# Golden dataset
# ---------------------------------------------------------------------------
def test_annotation_template_covers_the_pdfs(tmp_path: Path):
    dataset = tmp_path / "golden"
    write_samples(dataset)
    path = benchmark.write_annotation_template(dataset)

    rows = benchmark.load_annotations(dataset)
    assert path.exists()
    assert len(rows) == 3
    assert set(rows[0]) >= {"file_name", "go_number", "go_date", "department", "subject"}


def test_scoring_a_correctly_annotated_dataset_passes(tmp_path: Path):
    dataset = tmp_path / "golden"
    written = write_samples(dataset)
    benchmark.write_annotation_template(
        dataset, [sample.annotation(path.name) for sample, path in written]
    )

    report = benchmark.score_dataset(dataset)

    assert report.documents == 3
    assert report.phase1_pass
    for name in ("go_number", "go_date", "department", "subject"):
        assert report.scores[name].accuracy == 1.0


def test_scoring_detects_a_wrong_annotation(tmp_path: Path):
    dataset = tmp_path / "golden"
    written = write_samples(dataset)
    rows = [sample.annotation(path.name) for sample, path in written]
    rows[0]["go_number"] = "G.O.(Ms) No.999"
    benchmark.write_annotation_template(dataset, rows)

    report = benchmark.score_dataset(dataset)

    assert not report.phase1_pass
    assert report.scores["go_number"].wrong == 1
    assert report.scores["go_number"].mistakes[0]["kind"] == "wrong"


def test_hallucination_is_counted_separately(tmp_path: Path):
    """A value the annotator says is absent must not be produced."""
    dataset = tmp_path / "golden"
    written = write_samples(dataset)
    rows = [sample.annotation(path.name) for sample, path in written]
    for row in rows:
        row["district"] = ""  # claim no district appears anywhere
    benchmark.write_annotation_template(dataset, rows)

    report = benchmark.score_dataset(dataset)
    assert report.scores["district"].hallucinated == 3


def test_go_number_comparison_tolerates_formatting():
    assert benchmark.values_match("go_number", "G.O.(Ms) No.123", "G.O. Ms No. 123")
    assert not benchmark.values_match("go_number", "G.O.(Ms) No.123", "G.O.(Ms) No.124")
    assert not benchmark.values_match("go_number", "G.O.(Ms) No.123", "G.O.(Rt) No.123")


def test_date_comparison_tolerates_formats():
    assert benchmark.values_match("go_date", "15.03.2026", "2026-03-15")
    assert not benchmark.values_match("go_date", "15.03.2026", "2026-03-16")


def test_money_comparison_ignores_the_amount_in_words():
    assert benchmark.values_match(
        "budget", "Rs.2,45,00,000/- (Rupees Two Crore Forty Five Lakh only)", "24500000.00"
    )
    assert benchmark.values_match("budget", "Rs.12.50 crore", "125000000.00")
    assert not benchmark.values_match("budget", "Rs.12.50 crore", "12500000.00")
