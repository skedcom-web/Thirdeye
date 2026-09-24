"""Phase 4.1 -- GO Number OCR Coverage Recovery Program, Phase 2.

Evidence-backed regex additions from a root-cause investigation of 1,552
production records missing a GO number. Every case here is a real sample
pulled from production page-1 text (see the investigation's pattern
catalog) -- this file intentionally does NOT broaden coverage beyond those
documented cases; see patterns.py's own comments for why each tolerance
exists.
"""

from __future__ import annotations

from goengine.extraction import patterns as P
from goengine.extraction.metadata import extract_go_number
from goengine.extraction.text import PageText


# ---------------------------------------------------------------------------
# GO_NUMBER_FULL -- the three real punctuation/spacing variants
# ---------------------------------------------------------------------------
def test_matches_space_before_period_after_g():
    m = P.GO_NUMBER_FULL.search("G .0.(Ms).No.86")
    assert m is not None
    assert m.group("series") == "Ms"
    assert m.group("number") == "86"


def test_matches_comma_after_series_instead_of_period():
    m = P.GO_NUMBER_FULL.search("G.O.Ms,No.204")
    assert m is not None
    assert m.group("series2") == "Ms"
    assert m.group("number") == "204"


def test_matches_colon_between_no_and_number():
    m = P.GO_NUMBER_FULL.search("G.O.(Ms).No: 21")
    assert m is not None
    assert m.group("series") == "Ms"
    assert m.group("number") == "21"


def test_still_matches_previously_documented_ocr_variants():
    """Regression: the widened pattern must not lose any of the cases
    patterns.py's own comments already document as fixed."""
    assert P.GO_NUMBER_FULL.search("G.O.(Ms) No.123") is not None
    assert P.GO_NUMBER_FULL.search("G.0.(D).No.300 Dated: 04.03.2026") is not None
    assert P.GO_NUMBER_FULL.search("G.0.{Ms.) No.55") is not None


# ---------------------------------------------------------------------------
# GO_NUMBER_DOUBLE_MISREAD -- both letters of "G.O." misread as digits
# ---------------------------------------------------------------------------
def test_matches_both_letters_misread_as_zero():
    m = P.GO_NUMBER_DOUBLE_MISREAD.search("0.0. (Ms.) No.45")
    assert m is not None
    assert m.group("series") == "Ms"
    assert m.group("number") == "45"


def test_matches_second_letter_misread_as_six():
    m = P.GO_NUMBER_DOUBLE_MISREAD.search("0.6. (Ms.) No.54")
    assert m is not None
    assert m.group("series") == "Ms"
    assert m.group("number") == "54"


def test_double_misread_requires_series_and_no_never_fires_on_a_decimal_number():
    """The whole reason this is a separate, stricter pattern instead of
    widening GO_NUMBER_FULL's leading character class: an ordinary decimal
    number is common in these documents and must never be misread as a GO
    number just because it starts with 0.0/0.6."""
    decoys = [
        "the total cost is Rs. 0.06 per unit",
        "Section 0.6 of the Act applies",
        "GST rate revised from 0.6% to 0.0%",
        "page 0.0 of the document index",
        "Table 0.6: summary of allocations",
    ]
    for text in decoys:
        assert P.GO_NUMBER_DOUBLE_MISREAD.search(text) is None, text
        assert P.GO_NUMBER_FULL.search(text) is None, text


# ---------------------------------------------------------------------------
# End-to-end: extract_go_number() picks these up as real field candidates
# ---------------------------------------------------------------------------
def test_extract_go_number_recovers_double_misread_on_page_one():
    pages = [PageText(1, "ABSTRACT Goods and Services Tax - Orders Issued.\n"
                          "COMMERCIAL TAXES AND REGISTRATION (B1) DEPARTMENT\n"
                          "0.0. (Ms.) No.45 Dated: 31.01.2025")]
    candidates = extract_go_number(pages)
    assert candidates, "expected at least one go_number candidate"
    best = max(candidates, key=lambda c: c.confidence)
    assert best.normalized_value == "G.O.(Ms) No.45"
    assert best.method.startswith("GO_NUMBER_DOUBLE_MISREAD")


def test_extract_go_number_prefers_full_match_over_double_misread_when_both_present():
    """If a page genuinely contains both a real G.O. header and an unrelated
    misread-shaped decoy, the real header (GO_NUMBER_FULL, higher base
    confidence) must win -- not the weaker double-misread guess."""
    pages = [PageText(1, "G.O.(Ms) No.999 Dated: 01.01.2025\nSection 0.0. (Ms.) No.1 refers.")]
    candidates = extract_go_number(pages)
    from goengine.extraction.metadata import select_best

    best = select_best(candidates)
    assert best.normalized_value == "G.O.(Ms) No.999"
