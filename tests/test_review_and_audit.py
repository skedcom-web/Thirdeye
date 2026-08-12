"""Modules 7 & 8 -- verification decisions and the governance trail."""

from __future__ import annotations

import sqlite3

import pytest

from goengine import audit, review
from goengine.discovery import crawler
from goengine.pipeline import run_all


@pytest.fixture
def records(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    return [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]


def test_pipeline_produces_pending_records(conn, records, sample_pdfs):
    assert len(records) == len(sample_pdfs)
    assert review.counts_by_status(conn)["pending"] == len(sample_pdfs)


def test_only_approved_records_are_published(conn, records):
    assert review.verified_records(conn) == []

    review.approve(conn, records[0], reviewer="alex")
    published = review.verified_records(conn)

    assert [r["record_id"] for r in published] == [records[0]]
    assert published[0]["sha256"]
    assert published[0]["fields"]["go_number"]["source_page"] == 1


def test_approval_updates_the_discovery_status(conn, records):
    review.approve(conn, records[0], reviewer="alex")
    status = conn.execute(
        """
        SELECT dd.status FROM go_records r
          JOIN documents d ON d.id = r.document_id
          JOIN discovered_documents dd ON dd.id = d.discovered_id
         WHERE r.id = ?
        """,
        (records[0],),
    ).fetchone()["status"]
    assert status == crawler.STATUS_VERIFIED


def test_approval_requires_a_reviewer(conn, records):
    with pytest.raises(review.ReviewError):
        review.approve(conn, records[0], reviewer="")


def test_rejection_requires_a_reason(conn, records):
    with pytest.raises(review.ReviewError):
        review.reject(conn, records[0], reviewer="alex", reason="")


def test_cannot_approve_with_a_missing_core_field(conn, records):
    conn.execute(
        "DELETE FROM go_fields WHERE record_id = ? AND field_name = 'go_number'", (records[0],)
    )
    with pytest.raises(review.ReviewError, match="missing core fields"):
        review.approve(conn, records[0], reviewer="alex")

    # The override exists, but it is recorded.
    review.approve(conn, records[0], reviewer="alex", allow_missing_fields=True)
    entry = [e for e in audit.trail(conn, entity_type="go_record", entity_id=records[0])
             if e.action == "record.approved"][0]
    assert entry.detail["override_used"] is True
    assert entry.detail["missing_core_fields"] == ["go_number"]


def test_correction_supersedes_without_destroying_the_original(conn, records):
    record_id = records[0]
    before = review.get_summary(conn, record_id).fields["department"]

    review.correct_field(
        conn, record_id, "department", "Revenue and Disaster Management", reviewer="alex"
    )

    after = review.get_summary(conn, record_id).fields["department"]
    assert after["normalized_value"] == "Revenue and Disaster Management"
    assert after["origin"] == "corrected"
    assert after["created_by"] == "alex"

    # The machine-extracted row survives, marked superseded.
    original = conn.execute("SELECT * FROM go_fields WHERE id = ?", (before["id"],)).fetchone()
    assert original["superseded_by"] == after["id"]
    assert original["normalized_value"] == "Health and Family Welfare"


def test_correction_is_audited_with_before_and_after(conn, records):
    review.correct_field(conn, records[0], "department", "Finance", reviewer="alex", note="typo")
    entry = [e for e in audit.trail(conn, entity_type="go_record", entity_id=records[0])
             if e.action == "field.corrected"][0]

    assert entry.actor == "alex"
    assert entry.field_name == "department"
    assert entry.before_value == "Health and Family Welfare"
    assert entry.after_value == "Finance"
    assert entry.detail["note"] == "typo"


def test_corrected_field_still_needs_evidence(conn, records):
    conn.execute(
        "DELETE FROM go_fields WHERE record_id = ? AND field_name = 'scheme_name'", (records[0],)
    )
    with pytest.raises(review.ReviewError, match="source page is required"):
        review.correct_field(conn, records[0], "scheme_name", "Some Scheme", reviewer="alex")

    field_id = review.correct_field(
        conn, records[0], "scheme_name", "Some Scheme", reviewer="alex", source_page=1
    )
    assert field_id


def test_unknown_field_is_refused(conn, records):
    with pytest.raises(review.ReviewError, match="unknown field"):
        review.correct_field(conn, records[0], "chief_minister", "x", reviewer="alex")


def test_queue_orders_by_lowest_confidence(conn, records):
    rows = review.queue(conn)
    confidences = [row["min_field_confidence"] for row in rows]
    assert confidences == sorted(confidences)


# ---------------------------------------------------------------------------
# Module 8
# ---------------------------------------------------------------------------
def test_audit_log_is_append_only(conn, records):
    entry_id = audit.trail(conn, limit=1)[0].id

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_log SET actor = 'nobody' WHERE id = ?", (entry_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log WHERE id = ?", (entry_id,))


def test_provenance_covers_discovery_through_decision(conn, records):
    record_id = records[0]
    review.correct_field(conn, record_id, "department", "Finance", reviewer="alex")
    review.approve(conn, record_id, reviewer="alex", note="verified against the portal")

    document_id = review.get_summary(conn, record_id).document_id
    actions = [e.action for e in audit.document_provenance(conn, document_id)]

    for expected in (
        "document.discovered",
        "document.downloaded",
        "extraction.completed",
        "metadata.extracted",
        "field.corrected",
        "record.approved",
    ):
        assert expected in actions, f"{expected} missing from the provenance chain"


def test_every_published_field_traces_to_a_page_and_a_document(conn, records):
    review.approve(conn, records[0], reviewer="alex")
    published = review.verified_records(conn)[0]

    assert published["source_url"].startswith("https://cms.tn.gov.in/")
    assert len(published["sha256"]) == 64
    for name, data in published["fields"].items():
        assert data["source_page"] >= 1, name
        assert data["source_text"].strip(), name
