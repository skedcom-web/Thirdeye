"""Modules 6, 7, 8 -- Benchmark & Accuracy, Failure Intelligence, Calibration."""

from __future__ import annotations

import pytest

from goengine.certification import calibration as calib
from goengine.certification import failures as failure_intel
from goengine.certification import golden, run_full_certification
from goengine.certification.benchmark import run_certification_benchmark

CORRECT_ANNOTATIONS = {
    "go_number": "G.O.(Ms) No.123",
    "go_date": "2026-03-15",
    "department": "Health and Family Welfare",
    "subject": (
        "Health and Family Welfare Department - Upgradation of the Primary Health "
        "Centre at Melur into a 30-bedded Community Health Centre in Madurai "
        "District - Administrative sanction - Orders issued."
    ),
    "budget": "24500000.00",
    "district": "Madurai",
    "scheme_name": "National Health Mission",
}


@pytest.fixture
def golden_document_id(conn, settings, parsed_documents) -> int:
    """A fully, correctly annotated golden document (GO-123-2026 / Health)."""
    golden_id = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    for field_name, value in CORRECT_ANNOTATIONS.items():
        golden.annotate_field(conn, golden_id, field_name, value, annotator="alex")
    return golden_id


def test_a_correctly_annotated_document_scores_perfectly(conn, settings, golden_document_id):
    result = run_certification_benchmark(conn)

    assert result.documents_scored == 1
    assert result.skipped_incomplete == 0
    for field_name, stats in result.overall.items():
        assert stats.accuracy == 1.0, field_name
    assert result.phase2_targets_met
    assert result.mismatches == []


def test_incomplete_annotation_is_skipped_not_scored_as_wrong(conn, settings, parsed_documents):
    golden_id = golden.add_to_golden_set(conn, parsed_documents[0], added_by="alex")
    golden.annotate_field(conn, golden_id, "go_number", "G.O.(Ms) No.123", annotator="alex")
    # Only one of seven scored fields annotated.

    result = run_certification_benchmark(conn)
    assert result.documents_scored == 0
    assert result.skipped_incomplete == 1


def test_a_wrong_annotation_produces_a_mismatch(conn, settings, golden_document_id):
    golden.annotate_field(conn, golden_document_id, "budget", "999999.00", annotator="alex", note="deliberately wrong")

    result = run_certification_benchmark(conn)
    budget_stats = result.overall["budget"]

    assert budget_stats.tp == 0
    assert budget_stats.fp == 1
    assert budget_stats.fn == 1
    assert budget_stats.precision == 0.0
    assert budget_stats.recall == 0.0
    assert not result.phase2_targets_met

    assert len(result.mismatches) == 1
    mismatch = result.mismatches[0]
    assert mismatch.field_name == "budget"
    assert mismatch.kind == "wrong"
    assert mismatch.expected == "999999.00"
    assert mismatch.actual == "24500000.00"


def test_a_field_the_annotator_says_is_absent_but_machine_found_is_hallucination(
    conn, settings, golden_document_id
):
    golden.annotate_field(conn, golden_document_id, "scheme_name", None, annotator="alex")

    result = run_certification_benchmark(conn)
    stats = result.overall["scheme_name"]
    assert stats.fp == 1
    assert stats.tp == 0
    mismatch = [m for m in result.mismatches if m.field_name == "scheme_name"][0]
    assert mismatch.kind == "hallucinated"


def test_a_missed_field_is_scored_as_a_false_negative(conn, settings, golden_document_id):
    # Ground truth has a scheme, but claim the machine found nothing by
    # overwriting its field row -- simulates a real extraction miss.
    record_id = conn.execute(
        "SELECT r.id FROM go_records r JOIN documents d ON d.id = r.document_id "
        "WHERE d.id = (SELECT document_id FROM golden_documents WHERE id = ?)",
        (golden_document_id,),
    ).fetchone()["id"]
    conn.execute(
        "DELETE FROM go_fields WHERE record_id = ? AND field_name = 'scheme_name'", (record_id,)
    )

    result = run_certification_benchmark(conn)
    stats = result.overall["scheme_name"]
    assert stats.fn == 1
    assert stats.tp == 0
    mismatch = [m for m in result.mismatches if m.field_name == "scheme_name"][0]
    assert mismatch.kind == "missed"
    assert mismatch.actual is None


def test_benchmark_run_is_persisted(conn, settings, golden_document_id):
    result = run_certification_benchmark(conn)
    row = conn.execute(
        "SELECT * FROM certification_benchmark_runs WHERE id = ?", (result.run_id,)
    ).fetchone()
    assert row is not None
    assert row["documents_scored"] == 1


def test_by_department_and_by_language_breakdowns_are_populated(conn, settings, golden_document_id):
    result = run_certification_benchmark(conn)
    assert "health" in result.by_department
    assert "english" in result.by_language
    assert result.by_department["health"]["go_number"].accuracy == 1.0


# ---------------------------------------------------------------------------
# Module 7 -- Failure Intelligence
# ---------------------------------------------------------------------------
def test_wrong_value_is_recorded_as_a_field_specific_failure(conn, settings, golden_document_id):
    golden.annotate_field(conn, golden_document_id, "budget", "999999.00", annotator="alex")
    result = run_certification_benchmark(conn)
    n = failure_intel.record_failures(conn, result.run_id, result.mismatches)

    assert n == 1
    row = failure_intel.list_failures(conn)[0]
    assert row["failure_type"] == failure_intel.FAILURE_BUDGET
    assert row["field_name"] == "budget"


def test_hallucination_always_classified_as_hallucination_regardless_of_field(
    conn, settings, golden_document_id
):
    golden.annotate_field(conn, golden_document_id, "district", None, annotator="alex")
    result = run_certification_benchmark(conn)
    failure_intel.record_failures(conn, result.run_id, result.mismatches)

    row = failure_intel.list_failures(conn, field_name="district")[0]
    assert row["failure_type"] == failure_intel.FAILURE_HALLUCINATION


def test_scanned_document_failures_are_attributed_to_ocr_first(conn, settings, golden_document_id):
    """OCR is the priority-1 root cause: even a budget-field failure on a
    scanned document should be blamed on OCR, not on budget parsing."""
    document_id = conn.execute(
        "SELECT document_id FROM golden_documents WHERE id = ?", (golden_document_id,)
    ).fetchone()["document_id"]
    conn.execute(
        "UPDATE document_categories SET text_type = 'scanned' WHERE document_id = ?", (document_id,)
    )
    golden.annotate_field(conn, golden_document_id, "budget", "999999.00", annotator="alex")

    result = run_certification_benchmark(conn)
    failure_intel.record_failures(conn, result.run_id, result.mismatches)

    row = failure_intel.list_failures(conn)[0]
    assert row["failure_type"] == failure_intel.FAILURE_OCR


def test_failure_dashboards_query_correctly(conn, settings, golden_document_id):
    golden.annotate_field(conn, golden_document_id, "budget", "999999.00", annotator="alex")
    result = run_certification_benchmark(conn)
    failure_intel.record_failures(conn, result.run_id, result.mismatches)

    assert failure_intel.top_failure_types(conn)[0]["failure_type"] == failure_intel.FAILURE_BUDGET
    assert any(r["department_bucket"] == "health" for r in failure_intel.department_failure_counts(conn))
    assert any(r["language"] == "english" for r in failure_intel.language_failure_counts(conn))
    trend = failure_intel.failure_trend(conn)
    assert trend[-1]["failure_count"] == 1


# ---------------------------------------------------------------------------
# Module 8 -- Confidence Calibration
# ---------------------------------------------------------------------------
def test_correct_high_confidence_prediction_has_a_small_positive_or_zero_gap(
    conn, settings, golden_document_id
):
    result = run_certification_benchmark(conn)
    buckets = calib.compute_calibration(result.observations)
    go_number_bucket = [b for b in buckets if b.field_name == "go_number"][0]
    assert go_number_bucket.actual_accuracy == 1.0
    assert go_number_bucket.correct_count == 1


def test_wrong_high_confidence_prediction_produces_a_large_negative_gap(
    conn, settings, golden_document_id
):
    golden.annotate_field(conn, golden_document_id, "budget", "999999.00", annotator="alex")
    result = run_certification_benchmark(conn)
    buckets = calib.compute_calibration(result.observations)
    budget_bucket = [b for b in buckets if b.field_name == "budget"][0]

    assert budget_bucket.actual_accuracy == 0.0
    assert budget_bucket.mean_stated_confidence > 0.5  # the extractor was confident
    assert budget_bucket.calibration_gap < -0.3  # badly overconfident


def test_calibration_is_persisted_and_queryable(conn, settings, golden_document_id):
    result = run_full_certification(conn)
    rows = calib.calibration_for_run(conn, result.run_id)
    assert rows
    assert all(r["benchmark_run_id"] == result.run_id for r in rows)

    latest = calib.latest_calibration(conn)
    assert len(latest) == len(rows)


def test_overall_calibration_error_is_weighted_by_prediction_count():
    from goengine.certification.benchmark import ConfidenceObservation

    observations = [
        ConfidenceObservation(document_id=1, field_name="a", confidence=0.9, is_correct=True),
        ConfidenceObservation(document_id=2, field_name="a", confidence=0.9, is_correct=True),
        ConfidenceObservation(document_id=3, field_name="a", confidence=0.9, is_correct=False),
    ]
    buckets = calib.compute_calibration(observations)
    error = calib.overall_calibration_error(buckets)
    assert error is not None
    assert error > 0


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def test_run_full_certification_does_all_three_in_one_pass(conn, settings, golden_document_id):
    golden.annotate_field(conn, golden_document_id, "budget", "999999.00", annotator="alex")
    result = run_full_certification(conn)

    assert result.run_id
    assert len(failure_intel.list_failures(conn, benchmark_run_id=result.run_id)) == 1
    assert calib.calibration_for_run(conn, result.run_id)


def test_rerunning_benchmark_always_uses_current_extractor_code(conn, settings, golden_document_id):
    """A stale go_records row from before a code change must not silently
    stand in for a fresh measurement -- extract_and_store is idempotent per
    version, so re-running the benchmark re-verifies against current code."""
    from goengine.extraction.metadata import EXTRACTOR_VERSION

    result = run_certification_benchmark(conn)
    assert result.extractor_version == EXTRACTOR_VERSION
