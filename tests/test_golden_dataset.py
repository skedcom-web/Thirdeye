"""Module 3 -- Golden Dataset Workbench (real documents only)."""

from __future__ import annotations

import sqlite3

import pytest

from goengine.certification import golden


def test_only_a_real_archived_document_can_join_the_golden_set(conn, settings, parsed_documents):
    """Structural governance rule 3: no benchmarking against synthetic data.
    The FK to documents(id) means an id that never went through acquisition
    simply cannot be added."""
    with pytest.raises(LookupError):
        golden.add_to_golden_set(conn, 99999, added_by="alex")

    golden_id = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    assert golden_id


def test_adding_the_same_document_twice_is_idempotent(conn, settings, parsed_documents):
    first = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    second = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    assert first == second
    count = conn.execute("SELECT COUNT(*) AS n FROM golden_documents").fetchone()["n"]
    assert count == 1


def test_add_requires_an_annotator_identity(conn, settings, parsed_documents):
    with pytest.raises(golden.GoldenSetError):
        golden.add_to_golden_set(conn, parsed_documents[0], added_by="")


def test_annotation_records_ground_truth(conn, settings, parsed_documents):
    golden_id = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    golden.annotate_field(conn, golden_id, "go_number", "G.O.(Ms) No.123", annotator="alex")

    doc = golden.get_golden_document(conn, golden_id)
    assert doc.annotations["go_number"]["value"] == "G.O.(Ms) No.123"
    assert doc.annotations["go_number"]["annotator"] == "alex"


def test_absent_value_is_a_real_annotation_not_a_missing_one(conn, settings, parsed_documents):
    golden_id = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    golden.annotate_field(conn, golden_id, "district", None, annotator="alex", note="not mentioned")

    doc = golden.get_golden_document(conn, golden_id)
    assert "district" in doc.annotations  # a row exists
    assert doc.annotations["district"]["value"] is None  # asserting absence


def test_reannotation_supersedes_rather_than_overwrites(conn, settings, parsed_documents):
    golden_id = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    first_id = golden.annotate_field(conn, golden_id, "district", "Chennai", annotator="alex")
    second_id = golden.annotate_field(conn, golden_id, "district", "Madurai", annotator="alex")

    assert first_id != second_id
    original = conn.execute("SELECT * FROM golden_annotations WHERE id = ?", (first_id,)).fetchone()
    assert original["superseded_by"] == second_id
    assert original["value"] == "Chennai"  # the original judgement is retained, not lost

    current = golden.get_annotations(conn, golden_id)
    assert current["district"]["value"] == "Madurai"


def test_reannotating_with_the_same_value_is_a_noop(conn, settings, parsed_documents):
    golden_id = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    first_id = golden.annotate_field(conn, golden_id, "district", "Madurai", annotator="alex")
    second_id = golden.annotate_field(conn, golden_id, "district", "Madurai", annotator="alex")
    assert first_id == second_id


def test_unknown_field_is_refused(conn, settings, parsed_documents):
    golden_id = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    with pytest.raises(golden.GoldenSetError):
        golden.annotate_field(conn, golden_id, "chief_minister", "x", annotator="alex")


def test_is_complete_requires_all_seven_scored_fields(conn, settings, parsed_documents):
    golden_id = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    doc = golden.get_golden_document(conn, golden_id)
    assert not doc.is_complete

    for field_name in golden.SCORED_FIELDS:
        golden.annotate_field(conn, golden_id, field_name, "x", annotator="alex")

    doc = golden.get_golden_document(conn, golden_id)
    assert doc.is_complete
    # project_type is annotation-only and not required for completeness.
    assert "project_type" not in doc.annotated_fields


def test_golden_documents_cannot_be_deleted(conn, settings, parsed_documents):
    golden_id = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM golden_documents WHERE id = ?", (golden_id,))


def test_golden_annotations_cannot_be_deleted(conn, settings, parsed_documents):
    golden_id = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    field_id = golden.annotate_field(conn, golden_id, "district", "Madurai", annotator="alex")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM golden_annotations WHERE id = ?", (field_id,))


def test_candidates_prioritize_the_weakest_department_bucket(conn, settings, parsed_documents):
    """parsed_documents are health/education/public_works (1 each). Adding
    the health one to the golden set should push education/public_works
    ahead of nothing changing for an already-covered bucket."""
    candidates_before = golden.candidates_for_golden_set(conn)
    assert len(candidates_before) == 3

    golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")  # health
    candidates_after = golden.candidates_for_golden_set(conn)
    assert len(candidates_after) == 2
    assert parsed_documents[0] not in [c["document_id"] for c in candidates_after]


def test_golden_set_summary_counts_completeness(conn, settings, parsed_documents):
    golden_id = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    for field_name in golden.SCORED_FIELDS:
        golden.annotate_field(conn, golden_id, field_name, "x", annotator="alex")

    golden.add_to_golden_set(conn, parsed_documents[1], added_by="alex")  # left incomplete

    summary = golden.golden_set_summary(conn)
    assert summary["total"] == 2
    assert summary["fully_annotated"] == 1
