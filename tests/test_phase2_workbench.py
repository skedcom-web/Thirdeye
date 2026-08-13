"""Modules 3 & 9 -- Golden Dataset Workbench and Certification Dashboard (HTTP)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from goengine.certification import golden
from goengine.workbench.app import create_app, get_fetcher


@pytest.fixture
def client(conn, settings, parsed_documents, fetcher):
    app = create_app(settings)
    # Certification tests must stay offline like the rest of the suite --
    # override the route's real-network HttpFetcher with the same
    # OfflineFetcher already serving `parsed_documents`.
    app.dependency_overrides[get_fetcher] = lambda: fetcher
    return TestClient(app)


# ---------------------------------------------------------------------------
# Module 3
# ---------------------------------------------------------------------------
def test_golden_list_page_renders(client):
    response = client.get("/golden")
    assert response.status_code == 200
    assert "Real GO Acquisition Program" in response.text
    assert "GO-123-2026.pdf" in response.text  # a candidate, not yet golden


def test_add_to_golden_set_via_http(client, conn):
    response = client.post(
        "/golden/add", data={"document_id": 1, "added_by": "alex"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/golden/1"

    row = conn.execute("SELECT added_by FROM golden_documents WHERE document_id = 1").fetchone()
    assert row["added_by"] == "alex"


def test_add_nonexistent_document_returns_400(client):
    response = client.post("/golden/add", data={"document_id": 99999, "added_by": "alex"})
    assert response.status_code == 400


def test_annotate_via_http_shows_machine_suggestion_and_saves(client, conn):
    client.post("/golden/add", data={"document_id": 1, "added_by": "alex"})

    detail = client.get("/golden/1")
    assert detail.status_code == 200
    assert "machine:" in detail.text
    assert "G.O.(Ms) No.123" in detail.text  # the machine's own suggestion

    response = client.post(
        "/golden/1/annotate",
        data={"field_name": "go_number", "value": "G.O.(Ms) No.123", "annotator": "alex"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    annotations = golden.get_annotations(conn, 1)
    assert annotations["go_number"]["value"] == "G.O.(Ms) No.123"


def test_annotate_absent_checkbox_stores_none(client, conn):
    client.post("/golden/add", data={"document_id": 1, "added_by": "alex"})
    client.post(
        "/golden/1/annotate",
        data={"field_name": "scheme_name", "value": "should be ignored", "annotator": "alex", "absent": "1"},
    )
    annotations = golden.get_annotations(conn, 1)
    assert annotations["scheme_name"]["value"] is None


def test_missing_golden_document_is_404(client):
    assert client.get("/golden/9999").status_code == 404


# ---------------------------------------------------------------------------
# Module 9
# ---------------------------------------------------------------------------
def test_certification_dashboard_renders(client):
    response = client.get("/certification")
    assert response.status_code == 200
    assert "Source Certification" in response.text
    assert "Document Statistics" in response.text
    assert "Accuracy Metrics" in response.text
    assert "Confidence Calibration" in response.text
    assert "Failure Analytics" in response.text


def test_certification_dashboard_before_any_benchmark_run(client):
    """Must render cleanly with no benchmark history yet, not 500."""
    response = client.get("/certification")
    assert response.status_code == 200
    assert "No certification benchmark has run yet" in response.text


def test_run_benchmark_via_http_populates_the_dashboard(client, conn):
    golden_id = golden.add_to_golden_set(conn, 1, added_by="alex")
    for field_name in golden.SCORED_FIELDS:
        golden.annotate_field(conn, golden_id, field_name, "some value", annotator="alex")

    response = client.post("/certification/benchmark/run", follow_redirects=False)
    assert response.status_code == 303

    dashboard = client.get("/certification")
    assert "Run #1" in dashboard.text


def test_certify_source_via_http_uses_the_overridden_fetcher(client, conn):
    """The route's fetcher dependency is overridden with the offline fixture
    (same one that already served `parsed_documents`), so this exercises the
    real certification path with no network access.

    `parsed_documents` already downloaded every discovered document before
    this runs, so certification's download-sampling check has nothing new
    left to test -- PARTIALLY_CERTIFIED (not CERTIFIED) is the correct,
    expected outcome here, not a failure of the certification logic.
    """
    row = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()
    response = client.post(f"/certification/sources/{row['id']}/certify", follow_redirects=False)
    assert response.status_code == 303

    status = conn.execute(
        "SELECT certification_status FROM sources WHERE id = ?", (row["id"],)
    ).fetchone()["certification_status"]
    assert status == "PARTIALLY_CERTIFIED"


def test_certify_unknown_source_is_404(client):
    response = client.post("/certification/sources/9999/certify")
    assert response.status_code == 404
