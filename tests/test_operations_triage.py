"""Phase 4.1 -- triage classifier + Refinement 4 queue segmentation."""

from __future__ import annotations

from goengine.operations import triage


# ---------------------------------------------------------------------------
# classify_document_type -- pure function, advisory only (Refinement 10)
# ---------------------------------------------------------------------------
def test_classify_go_number_present_is_always_go():
    assert triage.classify_document_type("anything", "no GO pattern here at all", True) == "GO"


def test_classify_go_pattern_in_text_is_go_even_without_the_field():
    assert triage.classify_document_type(None, "This is G.O. (Ms) No. 42 dated today", False) == "GO"


def test_classify_recognizes_non_go_document_types():
    assert triage.classify_document_type("Weekly update", "This circular is issued for information.", False) == "Circular"
    assert triage.classify_document_type(None, "PRESS RELEASE: new scheme launched", False) == "Press Release"
    assert triage.classify_document_type(None, "Tender Notice for road works", False) == "Tender Notice"


def test_classify_falls_back_to_unknown():
    assert triage.classify_document_type(None, "completely unrelated scanned text", False) == "Unknown"


# ---------------------------------------------------------------------------
# Segmentation -- built on real parsed_documents fixtures
# ---------------------------------------------------------------------------
def _core_field_ids(conn, record_id: str, field_name: str):
    return [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM go_fields WHERE record_id = ? AND field_name = ? AND superseded_by IS NULL",
            (record_id, field_name),
        )
    ]


def _delete_field(conn, record_id: int, field_name: str) -> None:
    conn.execute(
        "DELETE FROM go_fields WHERE record_id = ? AND field_name = ? AND superseded_by IS NULL",
        (record_id, field_name),
    )


def _extraction_id_for(conn, record_id: int) -> int:
    return int(conn.execute("SELECT extraction_id FROM go_records WHERE id = ?", (record_id,)).fetchone()["extraction_id"])


def test_all_fields_present_records_are_ready_for_approval(conn, parsed_documents):
    counts = triage.review_segment_counts(conn)
    assert counts[triage.QUEUE_READY_FOR_APPROVAL] == 3
    assert sum(counts.values()) == 3


def test_missing_three_core_fields_is_critical_extraction_failure(conn, parsed_documents):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    for field in ("go_number", "go_date", "department"):
        _delete_field(conn, record_id, field)

    counts = triage.review_segment_counts(conn)
    assert counts[triage.QUEUE_CRITICAL] == 1
    assert sum(counts.values()) == 3  # still reconciles with total pending


def test_missing_one_or_two_core_fields_is_needs_correction(conn, parsed_documents):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    _delete_field(conn, record_id, "department")

    rows = triage.records_in_queue(conn, triage.QUEUE_NEEDS_CORRECTION)
    assert [r["record_id"] for r in rows] == [record_id]
    assert rows[0]["missing_field_names"] == ["department"]


def test_low_ocr_confidence_is_ocr_issues_even_with_all_fields_present(conn, parsed_documents):
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    extraction_id = _extraction_id_for(conn, record_id)
    conn.execute(
        """
        INSERT INTO ocr_runs (extraction_id, languages, pages_ocred, mean_word_confidence, ran_at)
        VALUES (?, 'eng', 1, 0.10, '2026-01-01T00:00:00+00:00')
        """,
        (extraction_id,),
    )

    counts = triage.review_segment_counts(conn)
    assert counts[triage.QUEUE_OCR_ISSUES] == 1
    assert sum(counts.values()) == 3


def test_non_go_text_with_missing_go_number_is_likely_non_go_not_needs_correction(conn, parsed_documents):
    """Priority order matters (Refinement 4): a record that is both missing
    a core field AND looks like a non-GO document lands in Likely Non-GO,
    not Needs Correction -- it needs a reject/reclassify decision, not a
    field correction."""
    record_id = conn.execute("SELECT MIN(id) AS id FROM go_records").fetchone()["id"]
    extraction_id = _extraction_id_for(conn, record_id)
    _delete_field(conn, record_id, "go_number")
    conn.execute(
        "UPDATE extraction_pages SET text = ? WHERE extraction_id = ? AND page_number = 1",
        ("This circular is issued to all departments for information.", extraction_id),
    )

    rows = triage.records_in_queue(conn, triage.QUEUE_LIKELY_NON_GO)
    assert [r["record_id"] for r in rows] == [record_id]
    assert rows[0]["suggested_reject_reason"] == "Circular"

    counts = triage.review_segment_counts(conn)
    assert counts[triage.QUEUE_NEEDS_CORRECTION] == 0
    assert sum(counts.values()) == 3


def test_department_filter_narrows_segmentation(conn, parsed_documents):
    from goengine import registry

    doc_id = parsed_documents[0]
    other_source_id = registry.add_source(
        conn, name="Health Department Site", department="Health and Family Welfare",
        url="https://cms.tn.gov.in/health", source_type="go_portal",
    )
    conn.execute("UPDATE go_records SET source_id = ? WHERE document_id = ?", (other_source_id, doc_id))

    scoped = triage.review_segment_counts(conn, department="Health and Family Welfare")
    assert sum(scoped.values()) == 1

    unscoped = triage.review_segment_counts(conn)
    assert sum(unscoped.values()) == 3


def test_records_in_queue_rejects_unknown_queue(conn, parsed_documents):
    import pytest

    with pytest.raises(ValueError):
        triage.records_in_queue(conn, "not_a_real_queue")


def test_top_blockers_sorts_by_missing_fields_then_confidence(conn, parsed_documents):
    ids = [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id")]
    _delete_field(conn, ids[0], "department")
    _delete_field(conn, ids[1], "department")
    _delete_field(conn, ids[1], "subject")

    blockers = triage.top_blockers(conn, limit=10)
    blocker_ids = [b["record_id"] for b in blockers]
    # ids[1] is missing 2 fields (worse) so it must rank above ids[0] (missing 1).
    assert blocker_ids.index(ids[1]) < blocker_ids.index(ids[0])
    # The untouched record (all fields present, Ready For Approval) is not a blocker.
    assert ids[2] not in blocker_ids
