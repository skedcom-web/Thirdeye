"""Phase 3.8 Initiative 2 -- Extraction Failure Workbench.

pipeline_failures() reads real audit_log entries that pipeline.py,
discovery/crawler.py, and acquisition.py already write on failure -- these
tests simulate those entries directly (via audit.record(), matching the
exact action/entity_type/detail shape the real code uses) rather than
forcing a real network failure, since the goal here is to prove the
query/join/stage-mapping logic, not re-test the pipeline itself.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from goengine import audit, review
from goengine.operations import failures
from goengine.pipeline import run_all
from goengine.workbench.app import create_app
from tests.conftest import login_as


def test_pipeline_failures_empty_with_nothing_recorded(conn, settings, source_id):
    assert failures.pipeline_failures(conn) == []


def test_discovery_failure_resolves_department_from_source(conn, settings, source_id):
    audit.record(
        conn, action="crawl.failed", entity_type="source", entity_id=source_id,
        detail={"error": "connection timed out"},
    )
    rows = failures.pipeline_failures(conn)
    assert len(rows) == 1
    assert rows[0]["stage"] == failures.STAGE_DISCOVERY
    assert rows[0]["department"] == "All Departments"
    assert rows[0]["error_message"] == "connection timed out"
    assert rows[0]["retry_url"] == "/ops/jobs?department=All%20Departments"


def test_download_failure_resolves_department_via_discovered_document(conn, settings, source_id):
    from goengine.db import utcnow

    discovered_id = conn.execute(
        """
        INSERT INTO discovered_documents
            (source_id, url, link_text, found_on_url, discovered_at, last_seen_at, status)
        VALUES (?, 'https://cms.tn.gov.in/x/go.pdf', '', '', ?, ?, 'new')
        """,
        (source_id, utcnow(), utcnow()),
    ).lastrowid
    audit.record(
        conn, action="document.download_failed", entity_type="discovered_document",
        entity_id=discovered_id, detail={"status": 404},
    )
    rows = failures.pipeline_failures(conn)
    assert len(rows) == 1
    assert rows[0]["stage"] == failures.STAGE_DOWNLOAD
    assert rows[0]["error_message"] == "HTTP 404"


def test_ocr_and_parse_failures_resolve_department_via_document(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    document_id = conn.execute("SELECT id FROM documents ORDER BY id LIMIT 1").fetchone()["id"]
    extraction_id = conn.execute(
        "SELECT id FROM extractions WHERE document_id = ?", (document_id,)
    ).fetchone()["id"]

    audit.record(
        conn, action="extraction.ocr_failed", entity_type="extraction", entity_id=extraction_id,
        detail={"error": "tesseract not available"},
    )
    audit.record(
        conn, action="parse.failed", entity_type="document", entity_id=document_id,
        detail={"error": "corrupt PDF"},
    )

    rows = failures.pipeline_failures(conn)
    stages = {r["stage"] for r in rows}
    assert stages == {failures.STAGE_OCR, failures.STAGE_PARSING}
    assert all(r["department"] == "All Departments" for r in rows)


def test_rejected_records_surface_as_publication_stage_failures(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    record_id = conn.execute("SELECT id FROM go_records ORDER BY id LIMIT 1").fetchone()["id"]
    review.reject(conn, record_id, reviewer="admin", reason="wrong department listed")

    rows = failures.pipeline_failures(conn)
    assert len(rows) == 1
    assert rows[0]["stage"] == failures.STAGE_PUBLICATION
    assert rows[0]["error_message"] == "wrong department listed"


def test_department_and_stage_filters_narrow_results(conn, settings, source_id):
    from goengine import registry

    other_source_id = registry.add_source(
        conn, name="Other Portal", department="Energy",
        url="https://cms.tn.gov.in/energy", source_type="go_portal",
    )
    audit.record(conn, action="crawl.failed", entity_type="source", entity_id=source_id, detail={"error": "a"})
    audit.record(conn, action="crawl.failed", entity_type="source", entity_id=other_source_id, detail={"error": "b"})

    assert len(failures.pipeline_failures(conn, department="Energy")) == 1
    assert len(failures.pipeline_failures(conn, department="Nonexistent Department")) == 0
    assert len(failures.pipeline_failures(conn, stage=failures.STAGE_DISCOVERY)) == 2
    assert len(failures.pipeline_failures(conn, stage=failures.STAGE_DOWNLOAD)) == 0


def test_results_sorted_newest_first(conn, settings, source_id):
    audit.record(conn, action="crawl.failed", entity_type="source", entity_id=source_id, detail={"error": "first"})
    audit.record(conn, action="crawl.failed", entity_type="source", entity_id=source_id, detail={"error": "second"})
    rows = failures.pipeline_failures(conn)
    assert [r["error_message"] for r in rows] == ["second", "first"]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def test_failures_page_renders_and_filters(conn, settings, source_id):
    audit.record(conn, action="crawl.failed", entity_type="source", entity_id=source_id, detail={"error": "connection reset"})

    client = TestClient(create_app(settings))
    login_as(client, conn)

    response = client.get("/ops/failures")
    assert response.status_code == 200
    assert "connection reset" in response.text
    assert "Extraction Failure Workbench" in response.text

    filtered = client.get("/ops/failures", params={"department": "All Departments", "stage": failures.STAGE_DISCOVERY})
    assert filtered.status_code == 200
    assert "connection reset" in filtered.text

    narrowed_out = client.get("/ops/failures", params={"stage": failures.STAGE_DOWNLOAD})
    assert narrowed_out.status_code == 200
    assert "connection reset" not in narrowed_out.text


def test_failures_page_rejects_unknown_department_and_stage(conn, settings, source_id):
    client = TestClient(create_app(settings))
    login_as(client, conn)
    assert client.get("/ops/failures", params={"department": "Not A Real Department"}).status_code == 400
    assert client.get("/ops/failures", params={"stage": "Not A Real Stage"}).status_code == 400
