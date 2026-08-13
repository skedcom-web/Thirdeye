"""Module 4 (OCR) and Module 5 (Tamil Language Processing)."""

from __future__ import annotations

from pathlib import Path

import pytest

from goengine.certification import language as lang
from goengine.certification.categorize import categorize_document, department_bucket_for
from goengine.extraction.ocr import apply_to_extraction, is_available
from goengine.extraction.text import extract_document, load_pages

requires_tesseract = pytest.mark.skipif(
    not is_available(), reason="Tesseract is not installed in this environment"
)


# ---------------------------------------------------------------------------
# Module 5 -- language classification (pure function, no dependencies)
# ---------------------------------------------------------------------------
def test_classifies_pure_english():
    result = lang.classify_text("This is a government order about health policy administration.")
    assert result.language == lang.LANGUAGE_ENGLISH


def test_classifies_pure_tamil():
    result = lang.classify_text("தமிழ்நாடு அரசு ஆணை எண் 123 சுகாதாரத் துறை")
    assert result.language == lang.LANGUAGE_TAMIL


def test_classifies_mixed():
    result = lang.classify_text(
        "G.O.(Ms) No.123 தமிழ்நாடு அரசு சுகாதாரத் துறை ஆணை Dated: 15.03.2026 Health Department Order"
    )
    assert result.language == lang.LANGUAGE_MIXED
    assert 0.3 < result.tamil_ratio < 0.7


def test_classifies_unknown_when_no_letters():
    result = lang.classify_text("12345 -- ..!! 2026")
    assert result.language == lang.LANGUAGE_UNKNOWN
    assert result.total_letters == 0


def test_english_boilerplate_does_not_flip_a_tamil_document():
    """TN GOs keep the G.O. number and department letterhead in English even
    in an otherwise fully Tamil-language order; that shouldn't read as 'mixed'."""
    mostly_tamil = "தமிழ்நாடு அரசு " * 20 + "G.O.(Ms) No.5"
    result = lang.classify_text(mostly_tamil)
    assert result.language == lang.LANGUAGE_TAMIL


def test_department_bucket_mapping():
    assert department_bucket_for("Health and Family Welfare") == "health"
    assert department_bucket_for("School Education") == "education"
    assert department_bucket_for("Public Works") == "public_works"
    assert department_bucket_for("Rural Development and Panchayat Raj") == "rural_development"
    assert department_bucket_for("Finance") == "other"
    assert department_bucket_for("") == "other"


# ---------------------------------------------------------------------------
# Module 2 -- categorization, built on Module 5
# ---------------------------------------------------------------------------
def test_categorize_document_reads_bucket_from_extracted_department_not_source(
    conn, settings, parsed_documents
):
    """The demo source is registered as 'All Departments' -- bucketing must
    come from the document's OWN extracted department, or every document
    from a general portal would incorrectly land in 'other'."""
    document_id = parsed_documents[0]  # GO-123-2026.pdf: Health and Family Welfare
    row = conn.execute(
        "SELECT department_bucket, language, text_type FROM document_categories WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    assert row["department_bucket"] == "health"
    assert row["language"] == "english"
    assert row["text_type"] == "digital"


def test_categorize_is_idempotent(conn, settings, parsed_documents):
    document_id = parsed_documents[0]
    extraction_id = conn.execute(
        "SELECT id FROM extractions WHERE document_id = ?", (document_id,)
    ).fetchone()["id"]
    pages = load_pages(conn, extraction_id)

    categorize_document(conn, document_id, extraction_id, pages)
    categorize_document(conn, document_id, extraction_id, pages)

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM document_categories WHERE document_id = ?", (document_id,)
    ).fetchone()["n"]
    assert count == 1


# ---------------------------------------------------------------------------
# Module 4 -- OCR, exercised against a genuinely scanned (image-only) PDF
# ---------------------------------------------------------------------------
def _render_image_only_pdf(path: Path, text: str) -> None:
    """A PDF with pixels but NO embedded text layer -- a real OCR test case,
    not a digital PDF the extractor could read directly."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    render_doc = pymupdf.open()
    render_page = render_doc.new_page(width=595, height=300)
    render_page.insert_textbox(pymupdf.Rect(20, 20, 575, 280), text, fontsize=16, fontname="helv")
    pix = render_page.get_pixmap(dpi=200)
    page.insert_image(pymupdf.Rect(20, 20, 575, 300), stream=pix.tobytes("png"))
    doc.save(path)
    doc.close()
    render_doc.close()


@requires_tesseract
def test_ocr_recovers_text_from_an_image_only_pdf(conn, settings, tmp_path):
    from goengine import registry, repository
    from goengine.db import utcnow

    text = (
        "GOVERNMENT OF TAMIL NADU\nABSTRACT\nHealth Department - Test scanned "
        "order - Orders issued.\nG.O.(Ms) No.999  Dated: 01.01.2026"
    )
    pdf_path = tmp_path / "scanned.pdf"
    _render_image_only_pdf(pdf_path, text)

    source_id = registry.add_source(
        conn, name="S", department="Health", url="https://cms.tn.gov.in/x", source_type="go_portal"
    )
    stored = repository.store(settings, pdf_path.read_bytes())
    discovered_id = conn.execute(
        """
        INSERT INTO discovered_documents
            (source_id, url, link_text, found_on_url, discovered_at, last_seen_at, status)
        VALUES (?, 'https://cms.tn.gov.in/x/go.pdf', '', '', ?, ?, 'new')
        """,
        (source_id, utcnow(), utcnow()),
    ).lastrowid
    document_id, _ = repository.record_document(
        conn, discovered_id=discovered_id, source_id=source_id,
        source_url="https://cms.tn.gov.in/x/go.pdf", file_name="go.pdf", stored=stored,
        content_type="application/pdf", http_status=200,
    )

    extraction_id = extract_document(conn, settings, document_id)
    before = load_pages(conn, extraction_id)
    assert before[0].char_count == 0  # confirms this really is a no-text-layer PDF

    result = apply_to_extraction(conn, extraction_id, repository.absolute_path(settings, stored.relative_path))

    assert result is not None
    assert result.mean_confidence > 0.5
    after = load_pages(conn, extraction_id)
    assert "GOVERNMENT OF TAMIL NADU" in after[0].text
    assert "G.O.(Ms) No.999" in after[0].text

    row = conn.execute(
        "SELECT ocr_applied, needs_ocr, confidence FROM extractions WHERE id = ?", (extraction_id,)
    ).fetchone()
    assert row["ocr_applied"] == 1
    assert row["needs_ocr"] == 0
    assert row["confidence"] > 0  # recomputed, not left at the pre-OCR 0.0


def test_ocr_is_a_noop_on_a_document_with_a_good_digital_text_layer(conn, settings, parsed_documents):
    document_id = parsed_documents[0]
    extraction_id = conn.execute(
        "SELECT id FROM extractions WHERE document_id = ?", (document_id,)
    ).fetchone()["id"]
    from goengine import repository

    path_row = conn.execute("SELECT stored_path FROM documents WHERE id = ?", (document_id,)).fetchone()
    result = apply_to_extraction(
        conn, extraction_id, repository.absolute_path(settings, path_row["stored_path"])
    )
    assert result is None  # nothing weak enough to OCR
