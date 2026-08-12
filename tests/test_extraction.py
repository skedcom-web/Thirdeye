"""Modules 5 & 6 -- text extraction and evidence-bound metadata."""

from __future__ import annotations

import pytest

from goengine.extraction import patterns as P
from goengine.extraction.metadata import (
    CORE_FIELDS,
    extract_metadata,
    page_regions,
    region_at,
)
from goengine.extraction.text import extract_file, normalize_text, score_confidence, PageText


# ---------------------------------------------------------------------------
# Module 5
# ---------------------------------------------------------------------------
def test_extraction_preserves_page_numbers(sample_pdfs):
    _, path = sample_pdfs[0]
    output = extract_file(path)

    assert output.page_count == 2
    assert [p.page_number for p in output.pages] == [1, 2]
    assert "ABSTRACT" in output.pages[0].text
    assert "Copy to:" in output.pages[1].text


def test_extraction_reports_backend_and_confidence(sample_pdfs):
    output = extract_file(sample_pdfs[0][1])
    assert output.backend in ("pymupdf", "pdfplumber", "pypdf")
    assert 0.0 < output.confidence <= 1.0
    assert output.needs_ocr is False
    assert output.log


def test_normalize_text_keeps_paragraphs_drops_noise():
    raw = "Line one\r\n\r\n\r\n\r\nLine    two\nLine three   "
    # Runs of blank lines collapse to one break; the paragraph break survives.
    assert normalize_text(raw) == "Line one\n\nLine two\nLine three"


def test_form_feed_becomes_a_line_break():
    assert normalize_text("Page one text\fPage two text") == "Page one text\nPage two text"


def test_empty_text_layer_is_flagged_for_ocr():
    pages = [PageText(1, ""), PageText(2, "")]
    confidence, needs_ocr = score_confidence(pages, "pymupdf")
    assert confidence == 0.0
    assert needs_ocr is True


def test_missing_file_raises(tmp_path):
    from goengine.extraction.text import ExtractionError

    with pytest.raises(ExtractionError):
        extract_file(tmp_path / "nope.pdf")


# ---------------------------------------------------------------------------
# Module 6
# ---------------------------------------------------------------------------
def test_core_fields_extracted_correctly(sample_pdfs):
    for sample, path in sample_pdfs:
        metadata = extract_metadata(extract_file(path).pages)

        assert metadata.value("go_number") == sample.go_number
        assert metadata.value("go_date") == sample.go_date
        assert metadata.value("department") == sample.department
        assert sample.subject.split(" - ")[1][:30] in metadata.value("subject")
        assert metadata.missing_core_fields == []


def test_every_field_carries_evidence(sample_pdfs):
    metadata = extract_metadata(extract_file(sample_pdfs[0][1]).pages)

    for name, candidate in metadata.fields.items():
        assert candidate.source_page >= 1, name
        assert candidate.source_text.strip(), name
        assert 0.0 <= candidate.confidence <= 1.0, name
        assert candidate.method, name


def test_the_orders_own_number_beats_a_cited_one(sample_pdfs):
    """The Read: block cites G.O.(Ms) No.11; the header carries No.123."""
    sample, path = sample_pdfs[0]
    pages = extract_file(path).pages
    metadata = extract_metadata(pages)

    assert metadata.value("go_number") == "G.O.(Ms) No.123"
    cited = [c for c in metadata.candidates
             if c.field_name == "go_number" and "No.11" in c.normalized_value]
    assert cited, "the cited order should still be recorded as a candidate"
    assert cited[0].confidence < metadata.fields["go_number"].confidence


def test_the_orders_own_date_beats_a_cited_one(sample_pdfs):
    metadata = extract_metadata(extract_file(sample_pdfs[0][1]).pages)
    assert metadata.value("go_date") == "2026-03-15"  # not the cited 2024-01-04


def test_region_detection():
    text = "HEADER LINE\nG.O.(Ms) No.5\n\nRead:\n 1. Another order\n\nORDER:\n\nBody text."
    bounds = page_regions(text)
    assert region_at(text.index("G.O."), bounds) == "header"
    assert region_at(text.index("Another"), bounds) == "references"
    assert region_at(text.index("Body"), bounds) == "body"


def test_nothing_is_invented_from_an_empty_document():
    metadata = extract_metadata([PageText(1, "This page contains no order details at all.")])
    assert metadata.fields == {}
    assert sorted(metadata.missing_core_fields) == sorted(CORE_FIELDS)


def test_future_dates_are_not_accepted():
    pages = [PageText(1, "Dated: 15.03.2099")]
    assert extract_metadata(pages).value("go_date") is None


def test_budget_units_are_expanded(sample_pdfs):
    by_name = {path.name: (sample, path) for sample, path in sample_pdfs}
    sample, path = by_name["GO-78-2026.pdf"]  # "Rs.12.50 crore"
    metadata = extract_metadata(extract_file(path).pages)
    assert float(metadata.value("budget")) == 125_000_000.0


def test_department_normalization():
    assert P.normalize_department("HEALTH AND FAMILY WELFARE (EAP-II) DEPARTMENT") == (
        "Health and Family Welfare"
    )
    assert P.normalize_department("SCHOOL EDUCATION DEPARTMENT") == "School Education"
    assert P.is_known_department("Public Works")


def test_district_normalization():
    assert P.normalize_district("Kanyakumari") == "Kanniyakumari"
    assert P.normalize_district("TUTICORIN") == "Thoothukudi"
    assert P.normalize_district("Notadistrict") is None


def test_scheme_name_does_not_swallow_preceding_clause():
    pages = [PageText(1, "works in Chennai District under the Integrated Urban Flood "
                         "Management Scheme during 2026.")]
    assert extract_metadata(pages).value("scheme_name") == (
        "Integrated Urban Flood Management Scheme"
    )
