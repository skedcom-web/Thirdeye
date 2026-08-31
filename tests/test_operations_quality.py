"""Next Phase Blueprint's extraction quality dashboard (operations/quality.py).

None of these metrics existed before -- each is checked against data whose
completeness is fully controlled, rather than assumed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from goengine import review
from goengine.operations import quality
from goengine.pipeline import run_all
from goengine.workbench.app import create_app
from tests.conftest import login_as


@pytest.fixture
def records(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    return [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]


def test_extraction_success_rate_with_clean_data(conn, records):
    # sampledata's 3 GOs are designed to parse with every core field present.
    result = quality.extraction_success_rate(conn)
    assert result["total"] == 3
    assert result["successful"] == 3
    assert result["rate"] == 100.0


def test_extraction_success_rate_reflects_a_missing_core_field(conn, records):
    conn.execute("DELETE FROM go_fields WHERE record_id = ? AND field_name = 'go_number'", (records[0],))
    result = quality.extraction_success_rate(conn)
    assert result["total"] == 3
    assert result["successful"] == 2
    assert result["rate"] == pytest.approx(66.7, abs=0.1)


def test_extraction_success_rate_with_no_records_is_zero_not_a_crash(conn):
    result = quality.extraction_success_rate(conn)
    assert result == {"total": 0, "successful": 0, "rate": 0.0}


def test_missing_metadata_breaks_down_by_field(conn, records):
    conn.execute("DELETE FROM go_fields WHERE record_id = ? AND field_name = 'budget'", (records[0],))
    rows = {r["field_name"]: r for r in quality.missing_metadata(conn)}
    assert rows["budget"]["missing"] >= 1
    assert rows["go_number"]["is_core"] is True
    assert rows["budget"]["is_core"] is False


def test_ocr_recovery_rate_scoped_to_needs_ocr_extractions(conn, records):
    # Simulate one extraction that needed OCR and still recovered all core
    # fields, and confirm the rate is scoped to needs_ocr=1 extractions only.
    conn.execute(
        "UPDATE extractions SET needs_ocr = 1 WHERE id = (SELECT extraction_id FROM go_records WHERE id = ?)",
        (records[0],),
    )
    result = quality.ocr_recovery_rate(conn)
    assert result["total"] == 1
    assert result["recovered"] == 1
    assert result["rate"] == 100.0


def test_ocr_recovery_rate_with_no_ocr_extractions_is_zero_not_a_crash(conn, records):
    result = quality.ocr_recovery_rate(conn)
    assert result == {"total": 0, "recovered": 0, "rate": 0.0}


def test_review_corrections_counts_by_field_and_recent_window(conn, records):
    assert quality.review_corrections(conn)["total"] == 0

    review.correct_field(conn, records[0], "department", "Revenue and Disaster Management", reviewer="alex")
    review.correct_field(conn, records[1], "department", "Finance", reviewer="alex")
    review.correct_field(conn, records[0], "subject", "A corrected subject", reviewer="alex")

    result = quality.review_corrections(conn)
    assert result["total"] == 3
    assert result["recent_count"] == 3
    by_field = {r["field_name"]: r["count"] for r in result["by_field"]}
    assert by_field["department"] == 2
    assert by_field["subject"] == 1


def test_department_coverage_counts_departments_with_an_approved_record(conn, records):
    from goengine import registry

    # source_id's department is "All Departments" (see conftest.py).
    assert quality.department_coverage(conn) == {"total": 1, "covered": 0, "rate": 0.0}

    review.approve(conn, records[0], reviewer="admin")
    assert quality.department_coverage(conn) == {"total": 1, "covered": 1, "rate": 100.0}

    registry.add_source(
        conn, name="Health Dept Source", department="Health and Family Welfare",
        url="https://www.tn.gov.in/go.php?dep_id=health", source_type="department_site",
    )
    result = quality.department_coverage(conn)
    assert result["total"] == 2
    assert result["covered"] == 1
    assert result["rate"] == 50.0


def test_quality_summary_combines_all_five_metrics(conn, records):
    summary = quality.quality_summary(conn)
    assert set(summary) == {
        "extraction_success", "ocr_recovery", "missing_metadata", "review_corrections", "department_coverage",
    }


# ---------------------------------------------------------------------------
# Phase 3.5 Initiative 2 -- GO Quality Scoring Engine
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "score, expected",
    [(100, "Excellent"), (90, "Excellent"), (89.9, "Good"), (75, "Good"),
     (74.9, "Needs Review"), (50, "Needs Review"), (49.9, "Poor"), (0, "Poor")],
)
def test_quality_category_boundaries(score, expected):
    assert quality.quality_category(score) == expected


def test_go_quality_score_with_everything_present_scores_highly(conn, settings, records):
    # sample #1 ("G.O.(Ms) No.123") has every core field plus all 3 optional
    # fields (budget/district/scheme_name) -- see sampledata.py -- and a
    # clean digital PDF with a real text layer, so extraction confidence
    # should be high and the file genuinely exists on disk.
    result = quality.go_quality_score(conn, settings, records[0])
    assert result["category"] == "Excellent"
    assert result["score"] >= 90
    assert result["breakdown"]["go_number"] == 20
    assert result["breakdown"]["pdf_availability"] == 15
    assert result["breakdown"]["metadata_completeness"] == 10  # all 3 optional fields present


def test_go_quality_score_loses_points_for_a_missing_core_field(conn, settings, records):
    conn.execute("DELETE FROM go_fields WHERE record_id = ? AND field_name = 'go_number'", (records[0],))
    result = quality.go_quality_score(conn, settings, records[0])
    assert result["breakdown"]["go_number"] == 0
    assert result["score"] < 90


def test_go_quality_score_loses_points_when_the_pdf_is_missing(conn, settings, records):
    row = conn.execute(
        "SELECT d.stored_path FROM documents d JOIN go_records r ON r.document_id = d.id WHERE r.id = ?",
        (records[0],),
    ).fetchone()
    from goengine import repository

    repository.absolute_path(settings, row["stored_path"]).unlink()

    result = quality.go_quality_score(conn, settings, records[0])
    assert result["breakdown"]["pdf_availability"] == 0


def test_go_quality_score_unknown_record_raises(conn, settings):
    with pytest.raises(LookupError):
        quality.go_quality_score(conn, settings, 9999)


def test_go_quality_score_zero_metadata_completeness_when_no_optional_fields(conn, settings, records):
    for field_name in ("budget", "district", "scheme_name"):
        conn.execute("DELETE FROM go_fields WHERE record_id = ? AND field_name = ?", (records[1], field_name))
    result = quality.go_quality_score(conn, settings, records[1])
    assert result["breakdown"]["metadata_completeness"] == 0


# ---------------------------------------------------------------------------
# Phase 3.5 Initiative 1 + 7 -- Department Health / Coverage KPIs
# ---------------------------------------------------------------------------
def test_department_health_reports_no_data_for_an_unextracted_department(conn, settings, records):
    from goengine import registry

    registry.add_source(
        conn, name="Untouched Dept Source", department="Untouched Department",
        url="https://www.tn.gov.in/go.php?dep_id=untouched", source_type="department_site",
    )
    health = {row["department"]: row for row in quality.department_health(conn, settings)}
    assert health["Untouched Department"]["status"] == "No Data"
    assert health["Untouched Department"]["total_gos"] == 0
    assert health["Untouched Department"]["quality_score"] is None


def test_department_health_reports_real_totals_and_score_for_an_extracted_department(conn, settings, records):
    # source_id's department is "All Departments" (see conftest.py).
    health = {row["department"]: row for row in quality.department_health(conn, settings)}
    row = health["All Departments"]
    assert row["total_gos"] == 3
    assert row["latest_go"] is not None
    assert row["last_extraction_date"] is not None
    assert row["quality_score"] is not None
    assert row["status"] in ("Excellent", "Good", "Needs Review", "Poor")


def test_department_coverage_kpis_counts_extracted_and_attention_needed(conn, settings, records):
    from goengine import registry

    registry.add_source(
        conn, name="Untouched Dept Source", department="Untouched Department",
        url="https://www.tn.gov.in/go.php?dep_id=untouched", source_type="department_site",
    )
    health = quality.department_health(conn, settings)
    kpis = quality.department_coverage_kpis(conn, registry.list_departments(conn), health)

    assert kpis["configured"] == 2
    assert kpis["extracted"] == 1  # "All Departments" only -- "Untouched Department" has 0 GOs
    assert kpis["requiring_attention"] >= 1  # "Untouched Department" (No Data)
    assert kpis["last_successful_extraction"] is None  # no completed extraction_requests row exists yet


def test_department_coverage_kpis_reflects_a_completed_extraction_run(conn, settings, records):
    from goengine import registry
    from goengine.operations import agent_auth, extraction_queue

    rid = extraction_queue.enqueue_local_request(
        conn, state_id=None, district_id=None, department_filter=None, created_by="admin",
    )
    key_id, _ = agent_auth.generate_key(conn, label="test", created_by="admin")
    extraction_queue.claim_next(conn, agent_key_id=key_id)
    extraction_queue.complete_request(conn, rid, ok=True)

    kpis = quality.department_coverage_kpis(
        conn, registry.list_departments(conn), quality.department_health(conn, settings)
    )
    assert kpis["last_successful_extraction"] is not None


# ---------------------------------------------------------------------------
# Phase 3.5 Initiative 3 -- Missing Metadata Workbench
# ---------------------------------------------------------------------------
def test_missing_metadata_queue_lists_only_records_with_a_real_gap(conn, settings, records):
    # All 3 sample records extract cleanly with a real PDF -- nothing should
    # appear in the queue yet.
    assert quality.missing_metadata_queue(conn, settings) == []

    conn.execute("DELETE FROM go_fields WHERE record_id = ? AND field_name = 'go_number'", (records[0],))
    queue = quality.missing_metadata_queue(conn, settings)
    assert len(queue) == 1
    assert queue[0]["record_id"] == records[0]
    assert queue[0]["missing_fields"] == ["go_number"]
    assert queue[0]["missing_pdf"] is False


def test_missing_metadata_queue_flags_a_missing_pdf(conn, settings, records):
    row = conn.execute(
        "SELECT d.stored_path FROM documents d JOIN go_records r ON r.document_id = d.id WHERE r.id = ?",
        (records[0],),
    ).fetchone()
    from goengine import repository

    repository.absolute_path(settings, row["stored_path"]).unlink()

    queue = quality.missing_metadata_queue(conn, settings)
    assert any(row["record_id"] == records[0] and row["missing_pdf"] for row in queue)


def test_missing_metadata_queue_excludes_approved_and_rejected_records(conn, settings, records):
    conn.execute("DELETE FROM go_fields WHERE record_id = ? AND field_name = 'go_number'", (records[0],))
    conn.execute("DELETE FROM go_fields WHERE record_id = ? AND field_name = 'go_date'", (records[1],))
    review.approve(conn, records[0], reviewer="admin", allow_missing_fields=True)
    review.reject(conn, records[1], reviewer="admin", reason="test")

    assert quality.missing_metadata_queue(conn, settings) == []


def test_missing_metadata_queue_filters_by_department(conn, settings, records):
    conn.execute("DELETE FROM go_fields WHERE record_id = ? AND field_name = 'go_number'", (records[0],))
    assert quality.missing_metadata_queue(conn, settings, department="All Departments") != []
    assert quality.missing_metadata_queue(conn, settings, department="Health and Family Welfare") == []


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def test_quality_page_renders(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    client = TestClient(create_app(settings))
    login_as(client, conn)

    response = client.get("/ops/quality")
    assert response.status_code == 200
    assert "Extraction Success Rate" in response.text
    assert "Department Health" in response.text
    assert "All Departments" in response.text  # the fixture source's department, in the health table


def test_metadata_workbench_lists_a_record_missing_a_field(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    record_id = int(conn.execute("SELECT id FROM go_records ORDER BY id LIMIT 1").fetchone()["id"])
    conn.execute("DELETE FROM go_fields WHERE record_id = ? AND field_name = 'go_number'", (record_id,))

    client = TestClient(create_app(settings))
    login_as(client, conn)
    response = client.get("/ops/metadata-workbench")
    assert response.status_code == 200
    assert f"/records/{record_id}" in response.text
    assert "Go Number" in response.text


def test_metadata_workbench_department_filter(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    record_id = int(conn.execute("SELECT id FROM go_records ORDER BY id LIMIT 1").fetchone()["id"])
    conn.execute("DELETE FROM go_fields WHERE record_id = ? AND field_name = 'go_number'", (record_id,))

    client = TestClient(create_app(settings))
    login_as(client, conn)

    matching = client.get("/ops/metadata-workbench", params={"department": "All Departments"})
    assert f"/records/{record_id}" in matching.text

    non_matching = client.get("/ops/metadata-workbench", params={"department": "Health and Family Welfare"})
    assert f"/records/{record_id}" not in non_matching.text


def test_reprocess_creates_a_fresh_pending_record_from_the_same_document(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    record_id = int(conn.execute("SELECT id FROM go_records ORDER BY id LIMIT 1").fetchone()["id"])
    review.approve(conn, record_id, reviewer="admin")

    client = TestClient(create_app(settings))
    login_as(client, conn)
    response = client.post(f"/records/{record_id}/reprocess", follow_redirects=False)
    assert response.status_code == 303

    new_record_id = int(response.headers["location"].rsplit("/", 1)[-1])
    assert new_record_id != record_id

    # The old, approved record is untouched -- reprocess never mutates it.
    old = conn.execute("SELECT status FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert old["status"] == "approved"
    new = conn.execute("SELECT status, document_id FROM go_records WHERE id = ?", (new_record_id,)).fetchone()
    assert new["status"] == "pending"

    old_doc = conn.execute("SELECT document_id FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert new["document_id"] == old_doc["document_id"]


def test_reprocess_requires_review_permission(conn, settings, fetcher, source_id):
    from goengine.operations import auth as ops_auth

    run_all(conn, settings, fetcher, only_due=False)
    record_id = int(conn.execute("SELECT id FROM go_records ORDER BY id LIMIT 1").fetchone()["id"])

    client = TestClient(create_app(settings))
    login_as(client, conn, username="readonlyuser", role=ops_auth.ROLE_READ_ONLY)
    response = client.post(f"/records/{record_id}/reprocess")
    assert response.status_code == 403


def test_reprocess_unknown_record_404s(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    client = TestClient(create_app(settings))
    login_as(client, conn)
    assert client.post("/records/9999/reprocess").status_code == 404
