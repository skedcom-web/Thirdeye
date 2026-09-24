"""Phase 4.1 -- OCR Coverage Recovery Program.

Covers both halves of operations/ocr_recovery.py:
- Agent-side gap detection/reset (find_unsynced_ocr_gaps/reset_for_resync),
  which closes the real sync-ordering bug found in a root-cause
  investigation: a document synced once right after download, whose local
  OCR (real Tesseract, agent-side) completed moments later, never had that
  OCR text actually transmitted because the sync gate had already closed.
- Server-side reporting (pending_recovery_summary/department_breakdown),
  which never runs OCR itself -- it only reports what the local agent still
  needs to deliver.
"""

from __future__ import annotations

from goengine import audit
from goengine.db import utcnow
from goengine.operations import ocr_recovery


def _insert_ocr_run(conn, extraction_id: int, *, ran_at: str, source: str = "server") -> None:
    conn.execute(
        """
        INSERT INTO ocr_runs (extraction_id, engine, engine_version, languages, pages_ocred,
                               mean_word_confidence, ran_at, log, source)
        VALUES (?, 'tesseract', '5.0', 'eng', 1, 0.9, ?, '', ?)
        """,
        (extraction_id, ran_at, source),
    )


# ---------------------------------------------------------------------------
# Agent-side: find_unsynced_ocr_gaps / reset_for_resync
# ---------------------------------------------------------------------------
def test_finds_a_document_whose_ocr_postdates_its_sync(conn, parsed_documents):
    document_id = parsed_documents[0]
    extraction_id = conn.execute(
        "SELECT extraction_id FROM go_records WHERE document_id = ?", (document_id,)
    ).fetchone()["extraction_id"]

    conn.execute("UPDATE documents SET agent_synced_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (document_id,))
    _insert_ocr_run(conn, extraction_id, ran_at="2026-01-02T00:00:00+00:00")  # after the sync

    gaps = ocr_recovery.find_unsynced_ocr_gaps(conn)
    assert document_id in gaps


def test_ignores_a_document_whose_ocr_predates_its_sync(conn, parsed_documents):
    """The normal, correct case: OCR ran, THEN the document was synced --
    nothing was lost, so this must not be flagged as a gap."""
    document_id = parsed_documents[0]
    extraction_id = conn.execute(
        "SELECT extraction_id FROM go_records WHERE document_id = ?", (document_id,)
    ).fetchone()["extraction_id"]

    _insert_ocr_run(conn, extraction_id, ran_at="2026-01-01T00:00:00+00:00")
    conn.execute("UPDATE documents SET agent_synced_at = '2026-01-02T00:00:00+00:00' WHERE id = ?", (document_id,))

    assert document_id not in ocr_recovery.find_unsynced_ocr_gaps(conn)


def test_ignores_a_document_with_no_ocr_run_at_all(conn, parsed_documents):
    document_id = parsed_documents[0]
    conn.execute("UPDATE documents SET agent_synced_at = ? WHERE id = ?", (utcnow(), document_id))
    assert document_id not in ocr_recovery.find_unsynced_ocr_gaps(conn)


def test_ignores_a_document_never_synced_at_all(conn, parsed_documents):
    """agent_synced_at IS NULL means it hasn't been pushed yet at all --
    the normal sync path will pick it up naturally, no recovery needed."""
    document_id = parsed_documents[0]
    extraction_id = conn.execute(
        "SELECT extraction_id FROM go_records WHERE document_id = ?", (document_id,)
    ).fetchone()["extraction_id"]
    _insert_ocr_run(conn, extraction_id, ran_at=utcnow())
    assert document_id not in ocr_recovery.find_unsynced_ocr_gaps(conn)


def test_source_ids_scopes_the_search(conn, parsed_documents, source_id):
    from goengine import registry

    document_id = parsed_documents[0]
    extraction_id = conn.execute(
        "SELECT extraction_id FROM go_records WHERE document_id = ?", (document_id,)
    ).fetchone()["extraction_id"]
    conn.execute("UPDATE documents SET agent_synced_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (document_id,))
    _insert_ocr_run(conn, extraction_id, ran_at="2026-01-02T00:00:00+00:00")

    other_source_id = registry.add_source(
        conn, name="Other Portal", department="Energy", url="https://cms.tn.gov.in/energy", source_type="go_portal",
    )
    assert document_id in ocr_recovery.find_unsynced_ocr_gaps(conn, source_ids=[source_id])
    assert document_id not in ocr_recovery.find_unsynced_ocr_gaps(conn, source_ids=[other_source_id])
    assert ocr_recovery.find_unsynced_ocr_gaps(conn, source_ids=[]) == []


def test_reset_for_resync_clears_sync_bookkeeping_only(conn, parsed_documents):
    document_id = parsed_documents[0]
    conn.execute("UPDATE documents SET agent_synced_at = ?, agent_sync_error = 'boom' WHERE id = ?", (utcnow(), document_id))

    touched = ocr_recovery.reset_for_resync(conn, [document_id])
    assert touched == 1

    row = conn.execute("SELECT agent_synced_at, agent_sync_error FROM documents WHERE id = ?", (document_id,)).fetchone()
    assert row["agent_synced_at"] is None
    # reset_for_resync only touches agent_synced_at -- the error field is a
    # separate concern (a real upload failure), left alone here.
    assert row["agent_sync_error"] == "boom"


def test_reset_for_resync_empty_list_is_a_no_op(conn, parsed_documents):
    assert ocr_recovery.reset_for_resync(conn, []) == 0


# ---------------------------------------------------------------------------
# Server-side: pending_recovery_summary / department_breakdown
# ---------------------------------------------------------------------------
def _make_record_need_ocr_skip(conn, record_id: int, extraction_id: int) -> None:
    """Reproduces the exact real-world signature: needs_ocr=1, an
    extraction.ocr_skipped audit event, and no ocr_runs row."""
    conn.execute("UPDATE extractions SET needs_ocr = 1 WHERE id = ?", (extraction_id,))
    audit.record(
        conn, action="extraction.ocr_skipped", entity_type="extraction", entity_id=extraction_id,
        detail={"reason": "tesseract not available"},
    )


def test_pending_summary_counts_only_the_documented_signature(conn, parsed_documents):
    record_id, extraction_id = conn.execute(
        "SELECT id, extraction_id FROM go_records WHERE document_id = ?", (parsed_documents[0],)
    ).fetchone()
    _make_record_need_ocr_skip(conn, record_id, extraction_id)

    summary = ocr_recovery.pending_recovery_summary(conn)
    assert summary["total_pending"] == 1


def test_pending_summary_excludes_records_with_a_real_ocr_run_already(conn, parsed_documents):
    """Once the agent's real OCR reaches the server (an ocr_runs row
    exists), this is no longer pending -- it's recovered."""
    record_id, extraction_id = conn.execute(
        "SELECT id, extraction_id FROM go_records WHERE document_id = ?", (parsed_documents[0],)
    ).fetchone()
    _make_record_need_ocr_skip(conn, record_id, extraction_id)
    _insert_ocr_run(conn, extraction_id, ran_at=utcnow(), source="local_agent")

    summary = ocr_recovery.pending_recovery_summary(conn)
    assert summary["total_pending"] == 0
    assert summary["recovered_total"] == 1


def test_pending_summary_excludes_needs_ocr_without_a_skip_event(conn, parsed_documents):
    """needs_ocr=1 alone isn't the signature -- a document that's still
    waiting its FIRST OCR attempt (no skip event yet) isn't a recovery
    target, just an in-progress one."""
    record_id, extraction_id = conn.execute(
        "SELECT id, extraction_id FROM go_records WHERE document_id = ?", (parsed_documents[0],)
    ).fetchone()
    conn.execute("UPDATE extractions SET needs_ocr = 1 WHERE id = ?", (extraction_id,))

    assert ocr_recovery.pending_recovery_summary(conn)["total_pending"] == 0


def test_department_breakdown_groups_by_source_department(conn, parsed_documents):
    record_id, extraction_id = conn.execute(
        "SELECT id, extraction_id FROM go_records WHERE document_id = ?", (parsed_documents[0],)
    ).fetchone()
    _make_record_need_ocr_skip(conn, record_id, extraction_id)

    breakdown = ocr_recovery.department_breakdown(conn)
    assert breakdown == [{"department": "All Departments", "pending": 1}]


def test_pending_records_for_department_filters_correctly(conn, parsed_documents):
    from goengine import registry

    record_id, extraction_id = conn.execute(
        "SELECT id, extraction_id FROM go_records WHERE document_id = ?", (parsed_documents[0],)
    ).fetchone()
    _make_record_need_ocr_skip(conn, record_id, extraction_id)

    assert len(ocr_recovery.pending_records_for_department(conn, "All Departments")) == 1
    assert len(ocr_recovery.pending_records_for_department(conn, "Health and Family Welfare")) == 0
