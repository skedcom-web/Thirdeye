"""Third Eye 4.0 Phase 1: My Area Dashboard / District Intelligence /
Financial Intelligence / Timeline Intelligence -- goengine/intelligence.py
and its /my-area, /districts HTTP surface.

The sample fixture (parsed_documents) ships 3 GOs in 3 different districts
(Madurai/health, Coimbatore/education, Chennai/public_works) with real
budget figures -- see goengine/sampledata.py::SAMPLES. Every test below
approves some subset of them directly via review.approve() rather than
going through HTTP, mirroring test_operations_review.py's style.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from goengine import intelligence, review
from goengine.workbench.app import create_app


def _all_record_ids(conn) -> list[int]:
    return [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]


def _approve_all(conn, record_ids=None) -> list[int]:
    ids = record_ids if record_ids is not None else _all_record_ids(conn)
    for record_id in ids:
        review.approve(conn, record_id, reviewer="tester")
    return ids


def _budget_of(conn, record_id: int) -> float:
    row = conn.execute(
        "SELECT normalized_value FROM go_fields WHERE record_id = ? AND field_name = 'budget' AND superseded_by IS NULL",
        (record_id,),
    ).fetchone()
    return float(row["normalized_value"])


def _district_of(conn, record_id: int) -> str:
    row = conn.execute(
        "SELECT normalized_value FROM go_fields WHERE record_id = ? AND field_name = 'district' AND superseded_by IS NULL",
        (record_id,),
    ).fetchone()
    return row["normalized_value"]


# ---------------------------------------------------------------------------
# format_inr
# ---------------------------------------------------------------------------
def test_format_inr_crore_and_lakh_and_plain():
    assert intelligence.format_inr(56_000_000) == "₹5.6 Crore"
    assert intelligence.format_inr(8_640_000) == "₹86.4 Lakh"
    assert intelligence.format_inr(50_000) == "₹50,000"
    assert intelligence.format_inr(500_000_000) == "₹50 Crore"  # no trailing .00


# ---------------------------------------------------------------------------
# area_summary -- approved-only gating, district scoping, empty state
# ---------------------------------------------------------------------------
def test_area_summary_none_when_nothing_approved(conn, parsed_documents):
    assert intelligence.area_summary(conn) is None


def test_area_summary_excludes_pending_records(conn, parsed_documents):
    ids = _all_record_ids(conn)
    review.approve(conn, ids[0], reviewer="tester")
    # ids[1:] remain pending.
    summary = intelligence.area_summary(conn)
    assert summary["government_initiatives"] == 1


def test_area_summary_district_scoping(conn, parsed_documents):
    ids = _approve_all(conn)
    target = ids[0]
    district = _district_of(conn, target)

    scoped = intelligence.area_summary(conn, district=district)
    assert scoped["government_initiatives"] == 1
    assert scoped["total_funds"] == pytest.approx(_budget_of(conn, target))

    statewide = intelligence.area_summary(conn, district=None)
    assert statewide["government_initiatives"] == len(ids)


def test_area_summary_none_for_district_with_no_approved_records(conn, parsed_documents):
    _approve_all(conn)
    assert intelligence.area_summary(conn, district="Nonexistent District") is None


def test_area_summary_latest_action_date_is_the_true_max(conn, parsed_documents):
    ids = _approve_all(conn)
    go_dates = []
    for rid in ids:
        row = conn.execute(
            "SELECT normalized_value FROM go_fields WHERE record_id = ? AND field_name = 'go_date' AND superseded_by IS NULL",
            (rid,),
        ).fetchone()
        go_dates.append(date.fromisoformat(row["normalized_value"]))

    summary = intelligence.area_summary(conn)
    assert summary["latest_action_date"] == max(go_dates)


def test_area_summary_departments_and_schemes(conn, parsed_documents):
    ids = _approve_all(conn)
    summary = intelligence.area_summary(conn)
    # 3 samples span 3 different department buckets (health/education/public_works).
    assert summary["departments_involved"] == 3

    actual_schemes = {
        row["normalized_value"]
        for rid in ids
        for row in conn.execute(
            "SELECT normalized_value FROM go_fields WHERE record_id = ? AND field_name = 'scheme_name' AND superseded_by IS NULL",
            (rid,),
        ).fetchall()
    }
    assert summary["schemes_available"] == len(actual_schemes)
    assert summary["schemes_available"] >= 1


# ---------------------------------------------------------------------------
# citizen_category_counts
# ---------------------------------------------------------------------------
def test_citizen_category_counts_matches_real_subject_keywords(conn, parsed_documents):
    _approve_all(conn)
    counts = {c["key"]: c["count"] for c in intelligence.citizen_category_counts(conn)}
    # Madurai sample's subject mentions "Health Centre" -> health category.
    assert counts.get("health") == 1
    # Coimbatore sample's subject mentions "Schools" -> edu category.
    assert counts.get("edu") == 1
    # Nothing in the fixture data should ever match every category.
    assert set(counts) != set(intelligence.SUBJECT_KEYWORDS)


def test_citizen_category_counts_omits_zero_categories(conn, parsed_documents):
    _approve_all(conn)
    counts = intelligence.citizen_category_counts(conn)
    assert all(c["count"] > 0 for c in counts)


def test_citizen_category_counts_empty_when_nothing_approved(conn, parsed_documents):
    assert intelligence.citizen_category_counts(conn) == []


# ---------------------------------------------------------------------------
# financial_breakdown
# ---------------------------------------------------------------------------
def test_financial_breakdown_by_department_sums_correctly(conn, parsed_documents):
    ids = _approve_all(conn)
    breakdown = intelligence.financial_breakdown(conn)
    assert breakdown is not None
    total_reported = sum(d["total"] for d in breakdown["by_department"])
    total_actual = sum(_budget_of(conn, rid) for rid in ids)
    assert total_reported == pytest.approx(total_actual)


def test_financial_breakdown_none_when_nothing_approved(conn, parsed_documents):
    assert intelligence.financial_breakdown(conn) is None


def test_financial_breakdown_monthly_trend_grouped_by_month(conn, parsed_documents):
    _approve_all(conn)
    breakdown = intelligence.financial_breakdown(conn)
    months = [m["month"] for m in breakdown["monthly_trend"]]
    assert months == sorted(months)  # oldest-to-newest for the chart
    assert all(m["total"] > 0 for m in breakdown["monthly_trend"])


# ---------------------------------------------------------------------------
# timeline_feed -- refinement #3: exactly department/subject/budget/go_date,
# newest first, no synthesized status
# ---------------------------------------------------------------------------
def test_timeline_feed_newest_first_with_only_verified_fields(conn, parsed_documents):
    ids = _approve_all(conn)
    entries = intelligence.timeline_feed(conn)
    assert len(entries) == len(ids)

    go_dates = [e["go_date"] for e in entries]
    assert go_dates == sorted(go_dates, reverse=True)

    for entry in entries:
        assert set(entry) == {"record_id", "department", "department_label", "subject", "budget", "go_date"}
        assert isinstance(entry["go_date"], date)


def test_timeline_feed_district_scoped(conn, parsed_documents):
    ids = _approve_all(conn)
    district = _district_of(conn, ids[0])
    entries = intelligence.timeline_feed(conn, district=district)
    assert len(entries) == 1


def test_timeline_feed_empty_when_nothing_approved(conn, parsed_documents):
    assert intelligence.timeline_feed(conn) == []


# ---------------------------------------------------------------------------
# district_ranking -- only districts with data, sortable
# ---------------------------------------------------------------------------
def test_district_ranking_only_includes_districts_with_approved_records(conn, parsed_documents):
    ids = _all_record_ids(conn)
    review.approve(conn, ids[0], reviewer="tester")  # only one district gets approved
    ranking = intelligence.district_ranking(conn)
    assert len(ranking) == 1
    assert ranking[0]["district"] == _district_of(conn, ids[0])


def test_district_ranking_sorts_by_funds_descending(conn, parsed_documents):
    _approve_all(conn)
    ranking = intelligence.district_ranking(conn, sort="funds")
    totals = [r["total_funds"] for r in ranking]
    assert totals == sorted(totals, reverse=True)


def test_district_ranking_empty_when_nothing_approved(conn, parsed_documents):
    assert intelligence.district_ranking(conn) == []


# ---------------------------------------------------------------------------
# HTTP surface -- /my-area, /districts
# ---------------------------------------------------------------------------
@pytest.fixture
def client(conn, settings):
    return TestClient(create_app(settings))


def test_my_area_never_says_active_projects(client, conn, parsed_documents):
    _approve_all(conn)
    response = client.get("/my-area")
    assert response.status_code == 200
    assert "Active Projects" not in response.text
    assert "Government Initiatives" in response.text


def test_my_area_shows_latest_government_action_card(client, conn, parsed_documents):
    _approve_all(conn)
    response = client.get("/my-area")
    assert "Latest Government Action" in response.text


def test_my_area_empty_state_exact_sentence_and_no_widgets(client, conn, parsed_documents):
    _approve_all(conn)
    response = client.get("/my-area?district=Nonexistent+District")
    assert response.status_code == 200
    assert intelligence.EMPTY_STATE_MESSAGE in response.text
    assert "Government Initiatives" not in response.text
    assert "Financial Intelligence" not in response.text


def test_my_area_timeline_entry_has_no_status_wording(client, conn, parsed_documents):
    _approve_all(conn)
    response = client.get("/my-area")
    for status_word in ("Tender", "In Progress", "Completed"):
        assert status_word not in response.text


def test_my_area_district_scoped_returns_200(client, conn, parsed_documents):
    ids = _approve_all(conn)
    district = _district_of(conn, ids[0])
    response = client.get(f"/my-area?district={district}")
    assert response.status_code == 200
    assert district in response.text


def test_districts_ranking_http_returns_200_and_links_to_my_area(client, conn, parsed_documents):
    ids = _approve_all(conn)
    district = _district_of(conn, ids[0])
    response = client.get("/districts")
    assert response.status_code == 200
    assert f"/my-area?district={district}" in response.text


def test_districts_ranking_empty_state(client, conn, parsed_documents):
    response = client.get("/districts")
    assert response.status_code == 200
    assert "No districts with approved government actions yet." in response.text


def test_my_area_nav_link_present_on_landing_page(client, conn):
    response = client.get("/")
    assert response.status_code == 200
    assert 'href="/my-area"' in response.text
