"""Phase 3.9 Initiatives 6 & 7 -- Published GO Edit & Republish Workflow."""

from __future__ import annotations

import pytest

from goengine import repository, review
from goengine.operations import republish
from goengine.pipeline import run_all


@pytest.fixture
def records(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    return [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]


@pytest.fixture
def approved_record(conn, records):
    review.approve(conn, records[0], reviewer="admin")
    return records[0]


def test_request_revision_requires_an_approved_record(conn, records):
    with pytest.raises(republish.RepublishError, match="published"):
        republish.request_revision(
            conn, records[0], editor="alex", changes={"subject": "Corrected subject"}, reason="typo"
        )


def test_request_revision_rejects_identity_critical_fields(conn, approved_record):
    for frozen_field in ("go_number", "go_date", "department"):
        with pytest.raises(republish.RepublishError, match="cannot be changed"):
            republish.request_revision(
                conn, approved_record, editor="alex", changes={frozen_field: "x"}, reason="test",
            )


def test_request_revision_snapshots_old_and_new_values(conn, approved_record):
    revision_id = republish.request_revision(
        conn, approved_record, editor="alex", changes={"subject": "New subject text"}, reason="typo fix",
    )
    history = republish.revision_history(conn, approved_record)
    assert len(history) == 1
    entry = history[0]
    assert entry["id"] == revision_id
    assert entry["status"] == republish.STATUS_DRAFT
    assert entry["version"] == 2
    change = entry["changes"][0]
    assert change["field_name"] == "subject"
    assert change["new_value"] == "New subject text"
    assert change["old_value"] is not None  # sample GOs always extract a subject


def test_submit_for_review_transitions_draft_to_pending(conn, approved_record):
    revision_id = republish.request_revision(
        conn, approved_record, editor="alex", changes={"subject": "New subject"}, reason="typo",
    )
    republish.submit_for_review(conn, revision_id, editor="alex")
    entry = republish.revision_history(conn, approved_record)[0]
    assert entry["status"] == republish.STATUS_PENDING_REVIEW


def test_approve_revision_requires_a_different_reviewer_than_requester(conn, settings, approved_record):
    revision_id = republish.request_revision(
        conn, approved_record, editor="alex", changes={"subject": "New subject"}, reason="typo",
    )
    with pytest.raises(republish.RepublishError, match="cannot approve their own"):
        republish.approve_revision(conn, revision_id, settings, reviewer="alex")


def test_approve_revision_applies_the_change_and_bumps_version(conn, settings, approved_record):
    revision_id = republish.request_revision(
        conn, approved_record, editor="alex", changes={"subject": "Corrected subject"}, reason="typo fix",
    )
    republish.approve_revision(conn, revision_id, settings, reviewer="admin")

    live_value = conn.execute(
        "SELECT normalized_value FROM go_fields WHERE record_id = ? AND field_name = 'subject' AND superseded_by IS NULL",
        (approved_record,),
    ).fetchone()["normalized_value"]
    assert live_value == "Corrected subject"

    version = conn.execute("SELECT current_version FROM go_records WHERE id = ?", (approved_record,)).fetchone()
    assert version["current_version"] == 2

    entry = republish.revision_history(conn, approved_record)[0]
    assert entry["status"] == republish.STATUS_REPUBLISHED
    assert entry["reviewed_by"] == "admin"
    assert entry["republished_at"] is not None


def test_approve_revision_never_changes_the_permanent_url(conn, settings, approved_record):
    """The critical guarantee: a republish edit to a non-identity field must
    never touch go_url_slug/canonical_go_id, even indirectly."""
    before = conn.execute(
        "SELECT go_url_slug, canonical_go_id FROM go_records WHERE id = ?", (approved_record,)
    ).fetchone()

    revision_id = republish.request_revision(
        conn, approved_record, editor="alex", changes={"subject": "A completely different subject"}, reason="fix",
    )
    republish.approve_revision(conn, revision_id, settings, reviewer="admin")

    after = conn.execute(
        "SELECT go_url_slug, canonical_go_id FROM go_records WHERE id = ?", (approved_record,)
    ).fetchone()
    assert after["go_url_slug"] == before["go_url_slug"]
    assert after["canonical_go_id"] == before["canonical_go_id"]
    assert before["go_url_slug"] is not None  # confirms this is a real, non-trivial check


def test_approve_revision_fails_when_source_pdf_is_missing(conn, settings, approved_record):
    stored_path = conn.execute(
        """
        SELECT d.stored_path FROM go_records r JOIN documents d ON d.id = r.document_id WHERE r.id = ?
        """,
        (approved_record,),
    ).fetchone()["stored_path"]
    repository.absolute_path(settings, stored_path).unlink()

    revision_id = republish.request_revision(
        conn, approved_record, editor="alex", changes={"subject": "New subject"}, reason="typo",
    )
    with pytest.raises(republish.RepublishError, match="source PDF"):
        republish.approve_revision(conn, revision_id, settings, reviewer="admin")


def test_reject_revision_leaves_live_data_untouched(conn, settings, approved_record):
    original_value = conn.execute(
        "SELECT normalized_value FROM go_fields WHERE record_id = ? AND field_name = 'subject' AND superseded_by IS NULL",
        (approved_record,),
    ).fetchone()["normalized_value"]

    revision_id = republish.request_revision(
        conn, approved_record, editor="alex", changes={"subject": "Should never apply"}, reason="typo",
    )
    republish.reject_revision(conn, revision_id, reviewer="admin", reason="not a real error")

    live_value = conn.execute(
        "SELECT normalized_value FROM go_fields WHERE record_id = ? AND field_name = 'subject' AND superseded_by IS NULL",
        (approved_record,),
    ).fetchone()["normalized_value"]
    assert live_value == original_value

    entry = republish.revision_history(conn, approved_record)[0]
    assert entry["status"] == republish.STATUS_REJECTED

    version = conn.execute("SELECT current_version FROM go_records WHERE id = ?", (approved_record,)).fetchone()
    assert version["current_version"] == 1


def test_revision_history_orders_newest_first(conn, settings, approved_record):
    rev1 = republish.request_revision(
        conn, approved_record, editor="alex", changes={"subject": "First edit"}, reason="a",
    )
    republish.approve_revision(conn, rev1, settings, reviewer="admin")
    rev2 = republish.request_revision(
        conn, approved_record, editor="alex", changes={"subject": "Second edit"}, reason="b",
    )
    history = republish.revision_history(conn, approved_record)
    assert [h["id"] for h in history] == [rev2, rev1]
    assert [h["version"] for h in history] == [3, 2]


def test_republish_status_before_any_revision(conn, approved_record):
    status = republish.republish_status(conn, approved_record)
    assert status["current_version"] == 1
    assert status["has_history"] is False
    assert status["last_republished_at"] is None


def test_republish_status_after_a_republish(conn, settings, approved_record):
    revision_id = republish.request_revision(
        conn, approved_record, editor="alex", changes={"subject": "Updated"}, reason="fix",
    )
    republish.approve_revision(conn, revision_id, settings, reviewer="admin")
    status = republish.republish_status(conn, approved_record)
    assert status["current_version"] == 2
    assert status["has_history"] is True
    assert status["last_republished_at"] is not None
