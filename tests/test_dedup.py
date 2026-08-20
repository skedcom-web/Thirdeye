"""Read-only duplicate-pending-record detection (operations/dedup.py) --
the diagnostic report for the resync-retry bug pipeline.py now prevents
going forward. Never deletes anything; that's a deliberate separate step."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from goengine.db import utcnow
from goengine.operations import dedup as ops_dedup
from goengine.workbench.app import create_app
from tests.conftest import login_as


def _add_duplicate_pending_record(conn, document_id: int) -> int:
    """Simulates exactly what a retried sync used to do: a second
    extractions row plus a second pending go_records row for a document
    that already has one, without touching the original."""
    row = conn.execute(
        "SELECT source_id FROM go_records WHERE document_id = ? LIMIT 1", (document_id,)
    ).fetchone()
    cur = conn.execute(
        """
        INSERT INTO extractions (document_id, backend, backend_version, page_count, char_count, confidence, needs_ocr, log, extracted_at)
        VALUES (?, 'pymupdf', '1.0', 1, 100, 0.9, 0, '', ?)
        """,
        (document_id, utcnow()),
    )
    extraction_id = int(cur.lastrowid)
    cur = conn.execute(
        """
        INSERT INTO go_records (extraction_id, document_id, source_id, extractor_version, status, created_at)
        VALUES (?, ?, ?, 'test-dup', 'pending', ?)
        """,
        (extraction_id, document_id, row["source_id"], utcnow()),
    )
    return int(cur.lastrowid)


def test_no_duplicates_in_a_clean_dataset(conn, parsed_documents):
    summary = ops_dedup.duplicate_summary(conn)
    assert summary["documents_with_duplicates"] == 0
    assert summary["records_removable"] == 0
    assert summary["groups"] == []


def test_finds_duplicate_and_identifies_the_newest_to_keep(conn, parsed_documents):
    document_id = parsed_documents[0]
    dup_id = _add_duplicate_pending_record(conn, document_id)

    summary = ops_dedup.duplicate_summary(conn)
    assert summary["documents_with_duplicates"] == 1
    assert summary["records_removable"] == 1

    group = summary["groups"][0]
    assert group.document_id == document_id
    assert dup_id in group.record_ids
    assert group.keep_id == max(group.record_ids)  # newest survives


def test_approved_records_are_never_counted_as_duplicates(conn, parsed_documents):
    from goengine import review

    document_id = parsed_documents[0]
    record_id = conn.execute("SELECT id FROM go_records WHERE document_id = ?", (document_id,)).fetchone()["id"]
    review.approve(conn, record_id, reviewer="admin")
    _add_duplicate_pending_record(conn, document_id)

    # Only the still-pending duplicate counts; the approved one is untouched
    # and must never be suggested for removal.
    summary = ops_dedup.duplicate_summary(conn)
    assert summary["documents_with_duplicates"] == 0  # only 1 pending record for this doc now


def test_department_breakdown_reflects_the_duplicate_documents_category(conn, parsed_documents):
    document_id = parsed_documents[0]
    bucket = conn.execute(
        "SELECT department_bucket FROM document_categories WHERE document_id = ?", (document_id,)
    ).fetchone()["department_bucket"]
    _add_duplicate_pending_record(conn, document_id)

    summary = ops_dedup.duplicate_summary(conn)
    assert summary["by_department"].get(bucket or "uncategorized") == 1


@pytest.fixture
def client(conn, settings, parsed_documents):
    test_client = TestClient(create_app(settings))
    login_as(test_client, conn)
    return test_client


def test_duplicates_report_via_http(client, conn, parsed_documents):
    document_id = parsed_documents[0]
    _add_duplicate_pending_record(conn, document_id)

    response = client.get("/ops/review/duplicates")
    assert response.status_code == 200
    assert "1" in response.text  # documents_with_duplicates stat renders

    # And it's genuinely read-only -- nothing is removed just by viewing it.
    summary_after = ops_dedup.duplicate_summary(conn)
    assert summary_after["documents_with_duplicates"] == 1


def test_duplicates_report_requires_manage_sources_permission(conn, settings, parsed_documents):
    client = TestClient(create_app(settings))
    login_as(client, conn, username="reviewer1", role="reviewer")
    response = client.get("/ops/review/duplicates")
    assert response.status_code == 403
