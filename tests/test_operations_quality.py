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
    assert "Extraction Coverage" in response.text
    assert "Success Rate" in response.text
    assert "Failure Rate" in response.text


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


# ---------------------------------------------------------------------------
# Phase 3.6 Initiative A -- Department Readiness Certification
# ---------------------------------------------------------------------------
def test_department_readiness_needs_attention_with_no_data(conn, settings, records):
    from goengine import registry

    registry.add_source(
        conn, name="Untouched Dept Source", department="Untouched Department",
        url="https://www.tn.gov.in/go.php?dep_id=untouched", source_type="department_site",
    )
    readiness = {row["department"]: row for row in quality.department_readiness(conn, settings)}
    row = readiness["Untouched Department"]
    assert row["status"] == "Needs Attention"
    assert row["total_gos"] == 0
    assert not any(row["checklist"].values())


def test_department_readiness_partially_ready_before_anything_is_approved(conn, settings, records):
    # sample data extracts cleanly (complete metadata, real PDFs, a computed
    # slug) but nothing is approved yet -- searchability specifically
    # requires an approved record (public.search() only ever returns
    # approved rows), so this must not be "Ready" yet.
    readiness = {row["department"]: row for row in quality.department_readiness(conn, settings)}
    row = readiness["All Departments"]
    assert row["status"] == "Partially Ready"
    assert row["checklist"]["latest_go_extracted"] is True
    assert row["checklist"]["historical_go_available"] is True
    assert row["checklist"]["pdf_available"] is True
    assert row["checklist"]["metadata_complete"] is True
    assert row["checklist"]["permanent_url_available"] is True
    assert row["checklist"]["searchable"] is False


def test_department_readiness_ready_once_something_is_approved_and_searchable(conn, settings, records):
    review.approve(conn, records[0], reviewer="admin")
    readiness = {row["department"]: row for row in quality.department_readiness(conn, settings)}
    row = readiness["All Departments"]
    assert row["checklist"]["searchable"] is True
    assert row["status"] == "Ready"


def test_department_readiness_partially_ready_when_a_core_field_is_missing(conn, settings, records):
    conn.execute("DELETE FROM go_fields WHERE record_id = ? AND field_name = 'go_number'", (records[0],))
    conn.execute("DELETE FROM go_fields WHERE record_id = ? AND field_name = 'go_number'", (records[1],))
    conn.execute("DELETE FROM go_fields WHERE record_id = ? AND field_name = 'go_number'", (records[2],))
    readiness = {row["department"]: row for row in quality.department_readiness(conn, settings)}
    row = readiness["All Departments"]
    assert row["checklist"]["metadata_complete"] is False
    assert row["status"] == "Partially Ready"


# ---------------------------------------------------------------------------
# Phase 3.6 Initiative E -- Publication Confidence Model
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "score, expected",
    [(100, "High Confidence"), (85, "High Confidence"), (84.9, "Medium Confidence"),
     (60, "Medium Confidence"), (59.9, "Review Recommended"), (0, "Review Recommended")],
)
def test_publication_confidence_label_thresholds(score, expected):
    assert quality.publication_confidence_label(score) == expected


def test_publication_confidence_never_exposes_the_raw_score(conn, settings, records):
    label = quality.publication_confidence(conn, settings, records[0])
    assert label in ("High Confidence", "Medium Confidence", "Review Recommended")
    assert isinstance(label, str)


# ---------------------------------------------------------------------------
# Phase 3.6 Initiative F -- Repository Health Dashboard
# ---------------------------------------------------------------------------
def test_repository_health_with_no_records(conn, settings):
    health = quality.repository_health(conn, settings)
    assert health["metadata_completeness_pct"] == 0.0
    assert health["pdf_availability_pct"] == 0.0
    assert health["search_indexing_coverage_pct"] == 0.0
    assert sum(health["publication_confidence_distribution"].values()) == 0


def test_repository_health_reflects_real_data(conn, settings, records):
    review.approve(conn, records[0], reviewer="admin")

    health = quality.repository_health(conn, settings)
    assert health["metadata_completeness_pct"] == quality.extraction_success_rate(conn)["rate"]
    assert health["pdf_availability_pct"] > 0
    assert sum(health["department_readiness"].values()) >= 1
    assert sum(health["publication_confidence_distribution"].values()) == 1  # exactly one approved record


def test_repository_health_confidence_distribution_matches_per_record_labels(conn, settings, records):
    for record_id in records:
        review.approve(conn, record_id, reviewer="admin")

    health = quality.repository_health(conn, settings)
    expected_counts = {"High Confidence": 0, "Medium Confidence": 0, "Review Recommended": 0}
    for record_id in records:
        expected_counts[quality.publication_confidence(conn, settings, record_id)] += 1
    assert health["publication_confidence_distribution"] == expected_counts


def test_repository_page_renders(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    record_id = int(conn.execute("SELECT id FROM go_records ORDER BY id LIMIT 1").fetchone()["id"])
    review.approve(conn, record_id, reviewer="admin")

    client = TestClient(create_app(settings))
    login_as(client, conn)
    response = client.get("/ops/repository")
    assert response.status_code == 200
    assert "Repository Analytics Center" in response.text
    assert "Department Readiness Certification" in response.text
    assert "Repository Health Dashboard" in response.text
    assert "All Departments" in response.text  # a real department appearing in the tables


# ---------------------------------------------------------------------------
# Phase 3.7 Initiative 7 -- Extraction Coverage Dashboard
# ---------------------------------------------------------------------------
def test_extraction_coverage_with_no_departments_or_requests_does_not_crash(conn, settings):
    kpis = quality.department_coverage_kpis(conn, [], [])
    coverage = quality.extraction_coverage(conn, kpis)
    assert coverage == {
        "departments_completed": 0, "departments_remaining": 0,
        "success_rate_pct": 0.0, "failure_rate_pct": 0.0,
        "latest_successful_extraction": None,
    }


def test_extraction_coverage_success_rate_reflects_real_extraction(conn, settings, records):
    from goengine import registry

    registry.add_source(
        conn, name="Untouched Dept Source", department="Untouched Department",
        url="https://www.tn.gov.in/go.php?dep_id=untouched", source_type="department_site",
    )
    departments = registry.list_departments(conn)
    health = quality.department_health(conn, settings)
    kpis = quality.department_coverage_kpis(conn, departments, health)

    coverage = quality.extraction_coverage(conn, kpis)
    assert coverage["departments_completed"] == 1  # "All Departments" has real GOs
    assert coverage["departments_remaining"] == 1  # "Untouched Department" does not
    assert coverage["success_rate_pct"] == 50.0


def test_extraction_coverage_failure_rate_is_request_level(conn, settings, records):
    from goengine.operations import agent_auth, extraction_queue

    key_id, _ = agent_auth.generate_key(conn, label="test", created_by="admin")

    ok_id = extraction_queue.enqueue_local_request(
        conn, state_id=None, district_id=None, department_filter=None, created_by="admin",
    )
    extraction_queue.claim_next(conn, agent_key_id=key_id)
    extraction_queue.complete_request(conn, ok_id, ok=True)

    failed_id = extraction_queue.enqueue_local_request(
        conn, state_id=None, district_id=None, department_filter=None, created_by="admin",
    )
    extraction_queue.claim_next(conn, agent_key_id=key_id)
    extraction_queue.complete_request(conn, failed_id, ok=False, error="no local sources matched")

    departments = ["All Departments"]
    health = quality.department_health(conn, settings)
    kpis = quality.department_coverage_kpis(conn, departments, health)
    coverage = quality.extraction_coverage(conn, kpis)

    assert coverage["failure_rate_pct"] == 50.0  # 1 of 2 requests failed
    assert coverage["latest_successful_extraction"] is not None
