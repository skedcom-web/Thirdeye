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
    response = client.get("/")
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
    """Refused either by FastAPI's own validation or by review.reject --
    which one depends on whether the client sends the empty field at all."""
    record_id = int(conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"])
    response = client.post(f"/records/{record_id}/reject", data={"reason": ""})

    assert response.status_code in (400, 422)
    assert review.get_summary(conn, record_id).status == "pending"


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
