"""Module 2 -- Real GO Acquisition Program (categorization + progress).

Computed automatically at parse time, for every archived document -- not
just ones later added to the golden set. This is what lets the acquisition
program be tracked ("how many Health orders do we have, how many are
scanned") without waiting for human annotation.

Department bucketing uses the document's SOURCE (the registered department
on `sources`), not the extracted `department` field: categorization runs
before metadata extraction (governance rule: language identified before
extraction begins), and bucketing by source is also the more stable signal
-- it doesn't depend on the extractor having got the field right.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .. import audit
from ..db import utcnow
from ..extraction.metadata import extract_department, select_best
from ..extraction.text import PageText
from . import language as lang

BUCKET_HEALTH = "health"
BUCKET_EDUCATION = "education"
BUCKET_PUBLIC_WORKS = "public_works"
BUCKET_RURAL_DEVELOPMENT = "rural_development"
BUCKET_OTHER = "other"

ALL_BUCKETS = (BUCKET_HEALTH, BUCKET_EDUCATION, BUCKET_PUBLIC_WORKS, BUCKET_RURAL_DEVELOPMENT)

# Real GO Acquisition Program targets from the Phase 2 blueprint.
ACQUISITION_TARGETS: dict[str, int] = {
    BUCKET_HEALTH: 50,
    BUCKET_EDUCATION: 50,
    BUCKET_PUBLIC_WORKS: 50,
    BUCKET_RURAL_DEVELOPMENT: 50,
}
ACQUISITION_TOTAL_TARGET = sum(ACQUISITION_TARGETS.values())  # 200

# Substring match against a source's registered department name. Order
# matters: checked top to bottom, first match wins.
_DEPARTMENT_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("health", BUCKET_HEALTH),
    ("family welfare", BUCKET_HEALTH),
    ("school education", BUCKET_EDUCATION),
    ("higher education", BUCKET_EDUCATION),
    ("education", BUCKET_EDUCATION),
    ("public works", BUCKET_PUBLIC_WORKS),
    ("rural development", BUCKET_RURAL_DEVELOPMENT),
    ("panchayat raj", BUCKET_RURAL_DEVELOPMENT),
)

ANNEXURE_RE = re.compile(r"\bANNEXURE\b", re.IGNORECASE)
ANNEXURE_PAGE_THRESHOLD = 5
ANNEXURE_MENTION_THRESHOLD = 2

# A "table-ish" line: three or more columns separated by two-plus spaces or a
# tab -- how pymupdf/pdfplumber render aligned numeric tables as plain text.
TABLE_LINE_RE = re.compile(r"(?:\S+(?:[ \t]{2,}|\t))(?:\S+(?:[ \t]{2,}|\t)){1,}\S+")
TABLE_LINE_THRESHOLD = 6


def department_bucket_for(department_name: str) -> str:
    lowered = (department_name or "").lower()
    for keyword, bucket in _DEPARTMENT_KEYWORDS:
        if keyword in lowered:
            return bucket
    return BUCKET_OTHER


# Confidence floor before an extracted department candidate is trusted over
# the source's registered department for bucketing purposes.
_DEPARTMENT_CANDIDATE_MIN_CONFIDENCE = 0.5


def _guess_department_bucket(pages: list[PageText], source_department: str) -> str:
    """Prefer the document's OWN department over its source's.

    A source like "Tamil Nadu GO Portal" is registered with department "All
    Departments" and spans every ministry, so bucketing purely by source
    would put every single order from it into 'other' -- exactly the sources
    a real acquisition program relies on most. This reuses the same pure
    page-scanning function Module 6 uses (no persistence, so it does not
    pre-empt or duplicate the governed metadata extraction that follows).
    """
    best = select_best(extract_department(pages))
    if best is not None and best.confidence >= _DEPARTMENT_CANDIDATE_MIN_CONFIDENCE:
        return department_bucket_for(best.normalized_value)
    return department_bucket_for(source_department)


@dataclass(frozen=True)
class DocumentCategory:
    document_id: int
    text_type: str  # digital | scanned
    language: str  # english | tamil | mixed | unknown
    tamil_char_ratio: float
    english_char_ratio: float
    annexure_heavy: bool
    table_heavy: bool
    department_bucket: str
    page_count: int


def _detect_annexure(pages: list[PageText]) -> bool:
    if len(pages) > ANNEXURE_PAGE_THRESHOLD:
        return True
    mentions = sum(len(ANNEXURE_RE.findall(p.text)) for p in pages)
    return mentions >= ANNEXURE_MENTION_THRESHOLD


def _detect_table_heavy(pages: list[PageText]) -> bool:
    table_lines = 0
    for page in pages:
        for line in page.text.splitlines():
            if TABLE_LINE_RE.search(line):
                table_lines += 1
    return table_lines >= TABLE_LINE_THRESHOLD


def categorize_document(
    conn: sqlite3.Connection,
    document_id: int,
    extraction_id: int,
    pages: list[PageText],
    *,
    actor: str = audit.SYSTEM_ACTOR,
) -> DocumentCategory:
    """Compute and persist a document's category. Idempotent: re-running
    replaces the prior row rather than accumulating duplicates."""
    extraction_row = conn.execute(
        "SELECT needs_ocr, ocr_applied, page_count FROM extractions WHERE id = ?",
        (extraction_id,),
    ).fetchone()
    if extraction_row is None:
        raise LookupError(f"no extraction with id {extraction_id}")

    # A document that needed OCR, or got it, had no usable digital text layer
    # to begin with -- that is what "scanned" means here, regardless of how
    # well the recovered text later reads.
    text_type = "scanned" if (extraction_row["needs_ocr"] or extraction_row["ocr_applied"]) else "digital"

    source_department = conn.execute(
        """
        SELECT s.department FROM documents d JOIN sources s ON s.id = d.source_id
         WHERE d.id = ?
        """,
        (document_id,),
    ).fetchone()
    bucket = _guess_department_bucket(
        pages, source_department["department"] if source_department else ""
    )

    language_result = lang.classify_pages(pages)
    annexure_heavy = _detect_annexure(pages)
    table_heavy = _detect_table_heavy(pages)
    page_count = int(extraction_row["page_count"])

    conn.execute("DELETE FROM document_categories WHERE document_id = ?", (document_id,))
    conn.execute(
        """
        INSERT INTO document_categories
            (document_id, text_type, language, tamil_char_ratio, english_char_ratio,
             annexure_heavy, table_heavy, department_bucket, page_count, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id, text_type, language_result.language,
            language_result.tamil_ratio, language_result.english_ratio,
            int(annexure_heavy), int(table_heavy), bucket, page_count, utcnow(),
        ),
    )

    audit.record(
        conn,
        action="document.categorized",
        entity_type="document",
        entity_id=document_id,
        actor=actor,
        detail={
            "text_type": text_type,
            "language": language_result.language,
            "department_bucket": bucket,
            "annexure_heavy": annexure_heavy,
            "table_heavy": table_heavy,
        },
    )

    return DocumentCategory(
        document_id=document_id, text_type=text_type, language=language_result.language,
        tamil_char_ratio=language_result.tamil_ratio, english_char_ratio=language_result.english_ratio,
        annexure_heavy=annexure_heavy, table_heavy=table_heavy,
        department_bucket=bucket, page_count=page_count,
    )


def get_category(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM document_categories WHERE document_id = ?", (document_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# Acquisition program progress
# ---------------------------------------------------------------------------
def acquisition_progress(conn: sqlite3.Connection) -> dict:
    """Real-document counts against the 200-GO / 4-department target.

    Counts every archived document that has been categorized -- the raw
    acquisition program, independent of golden-set membership or review status.
    """
    rows = conn.execute(
        "SELECT department_bucket, COUNT(*) AS n FROM document_categories GROUP BY department_bucket"
    ).fetchall()
    by_bucket = {row["department_bucket"]: int(row["n"]) for row in rows}

    departments = {
        bucket: {
            "count": by_bucket.get(bucket, 0),
            "target": target,
            "met": by_bucket.get(bucket, 0) >= target,
        }
        for bucket, target in ACQUISITION_TARGETS.items()
    }
    total_in_target_buckets = sum(d["count"] for d in departments.values())
    other_count = by_bucket.get(BUCKET_OTHER, 0)

    text_type_rows = conn.execute(
        "SELECT text_type, COUNT(*) AS n FROM document_categories GROUP BY text_type"
    ).fetchall()
    language_rows = conn.execute(
        "SELECT language, COUNT(*) AS n FROM document_categories GROUP BY language"
    ).fetchall()

    return {
        "departments": departments,
        "total_in_target_departments": total_in_target_buckets,
        "total_target": ACQUISITION_TOTAL_TARGET,
        "target_met": total_in_target_buckets >= ACQUISITION_TOTAL_TARGET
        and all(d["met"] for d in departments.values()),
        "other_department_documents": other_count,
        "by_text_type": {row["text_type"]: int(row["n"]) for row in text_type_rows},
        "by_language": {row["language"]: int(row["n"]) for row in language_rows},
        "annexure_heavy_count": conn.execute(
            "SELECT COUNT(*) AS n FROM document_categories WHERE annexure_heavy = 1"
        ).fetchone()["n"],
        "table_heavy_count": conn.execute(
            "SELECT COUNT(*) AS n FROM document_categories WHERE table_heavy = 1"
        ).fetchone()["n"],
    }
