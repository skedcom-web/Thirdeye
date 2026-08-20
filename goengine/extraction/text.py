"""Module 5 -- PDF Text Extraction Engine.

Produces page-addressable text: every character downstream can be pointed back
to a printed page number, which is what makes field-level evidence possible.

Backends are tried in quality order (pymupdf > pdfplumber > pypdf). The one
that actually ran is recorded on the extraction row, because extraction
quality is part of the evidence.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import audit
from ..config import Settings
from ..db import utcnow
from ..repository import absolute_path

# Below this many characters per page we assume a scanned image, not text.
MIN_CHARS_PER_PAGE_FOR_TEXT = 80


@dataclass
class PageText:
    page_number: int  # 1-based
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class ExtractionOutput:
    backend: str
    backend_version: str
    pages: list[PageText]
    confidence: float
    needs_ocr: bool
    log: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def char_count(self) -> int:
        return sum(p.char_count for p in self.pages)

    @property
    def full_text(self) -> str:
        return "\n".join(p.text for p in self.pages)


class ExtractionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
def _extract_pymupdf(path: Path) -> tuple[list[PageText], str]:
    import pymupdf  # type: ignore

    pages: list[PageText] = []
    with pymupdf.open(path) as doc:
        for index, page in enumerate(doc, start=1):
            # "blocks" sort keeps reading order sane on the two-column layouts
            # common in GO annexures.
            pages.append(PageText(index, page.get_text("text", sort=True)))
    return pages, getattr(pymupdf, "__version__", "unknown")


def _extract_pdfplumber(path: Path) -> tuple[list[PageText], str]:
    import pdfplumber  # type: ignore

    pages: list[PageText] = []
    with pdfplumber.open(path) as doc:
        for index, page in enumerate(doc.pages, start=1):
            pages.append(PageText(index, page.extract_text() or ""))
    return pages, getattr(pdfplumber, "__version__", "unknown")


def _extract_pypdf(path: Path) -> tuple[list[PageText], str]:
    import pypdf  # type: ignore

    reader = pypdf.PdfReader(str(path))
    pages = [
        PageText(index, page.extract_text() or "")
        for index, page in enumerate(reader.pages, start=1)
    ]
    return pages, getattr(pypdf, "__version__", "unknown")


BACKENDS = (
    ("pymupdf", _extract_pymupdf),
    ("pdfplumber", _extract_pdfplumber),
    ("pypdf", _extract_pypdf),
)


def normalize_text(raw: str) -> str:
    """Tidy whitespace while preserving paragraph breaks.

    Blank lines carry structure in a GO (the abstract block, the numbered
    order paragraphs), so they survive; runs of spaces and stray form feeds
    do not.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def score_confidence(pages: list[PageText], backend: str) -> tuple[float, bool]:
    """Heuristic 0..1 extraction confidence, plus an OCR-needed flag.

    This scores how well the TEXT LAYER came out -- not whether the extracted
    field values are right. A born-digital GO scores high; a scanned one
    scores near zero and is flagged for OCR rather than silently parsed.
    """
    if not pages:
        return 0.0, True

    total_chars = sum(p.char_count for p in pages)
    chars_per_page = total_chars / len(pages)
    empty_pages = sum(1 for p in pages if p.char_count < MIN_CHARS_PER_PAGE_FOR_TEXT)
    empty_ratio = empty_pages / len(pages)

    if total_chars == 0:
        return 0.0, True

    # Density: ~1200 chars/page is a well-extracted text page. GOs run short --
    # the final page is often just an address block -- so the bar is set at
    # what a real order page looks like, not at a dense report page.
    density_score = min(chars_per_page / 1200.0, 1.0)
    coverage_score = 1.0 - empty_ratio

    # A weak backend is a real source of extraction error, so it caps the score.
    backend_ceiling = {"pymupdf": 1.0, "pdfplumber": 0.97, "pypdf": 0.90}.get(backend, 0.85)

    # Replacement chars mean a decoding problem, which matters a lot for the
    # Tamil text in these orders.
    sample = "".join(p.text for p in pages[:5])
    garble_penalty = 0.0
    if sample:
        garble_penalty = min(sample.count("�") / max(len(sample), 1) * 20, 0.5)

    confidence = (0.6 * density_score + 0.4 * coverage_score) * backend_ceiling
    confidence = max(0.0, min(confidence - garble_penalty, 1.0))
    needs_ocr = chars_per_page < MIN_CHARS_PER_PAGE_FOR_TEXT or empty_ratio > 0.5
    return round(confidence, 4), needs_ocr


def extract_file(
    path: Path, *, preferred_backend: str | None = None, try_alternates: bool = True,
) -> ExtractionOutput:
    """Extract text from a PDF, falling back through the backend list.

    `try_alternates=False` skips that fallback hunt entirely -- for a
    genuinely scanned document, every backend agrees "needs OCR" anyway, so
    the hunt just means paying for a second and third full parse of the same
    file to arrive at the same answer. Worth it when digital-text quality is
    the only source of truth (the normal case); wasted, and on a large
    image-heavy scan expensive enough to matter, when the caller already has
    real OCR text lined up to override this result regardless of which
    backend produced it -- see pipeline.parse_document's precomputed_ocr path.
    """
    if not path.exists():
        raise ExtractionError(f"file not found: {path}")

    candidates = list(BACKENDS)
    if preferred_backend:
        candidates.sort(key=lambda item: item[0] != preferred_backend)

    log: list[str] = []
    for name, function in candidates:
        try:
            raw_pages, version = function(path)
        except ImportError:
            log.append(f"{name}: not installed, skipped")
            continue
        except Exception as exc:  # a malformed PDF should fall through, not crash
            log.append(f"{name}: failed ({type(exc).__name__}: {exc})")
            continue

        pages = [PageText(p.page_number, normalize_text(p.text)) for p in raw_pages]
        confidence, needs_ocr = score_confidence(pages, name)
        log.append(
            f"{name} {version}: {len(pages)} pages, "
            f"{sum(p.char_count for p in pages)} chars, confidence {confidence}"
        )

        if try_alternates and needs_ocr and any(other != name for other, _ in candidates):
            # A near-empty result may be this backend's fault rather than a
            # scanned document; note it and let the loop try the next one.
            log.append(f"{name}: sparse text layer, trying next backend")
            best_alternative = _try_remaining(path, name, candidates, log)
            if best_alternative is not None and not best_alternative.needs_ocr:
                best_alternative.log = log + best_alternative.log
                return best_alternative

        return ExtractionOutput(
            backend=name,
            backend_version=version,
            pages=pages,
            confidence=confidence,
            needs_ocr=needs_ocr,
            log=log,
        )

    raise ExtractionError(
        "no PDF backend could read the file; tried: " + "; ".join(log or ["none available"])
    )


def _try_remaining(
    path: Path, current: str, candidates: list, log: list[str]
) -> ExtractionOutput | None:
    for name, function in candidates:
        if name == current:
            continue
        try:
            raw_pages, version = function(path)
        except Exception as exc:
            log.append(f"{name}: failed ({type(exc).__name__}: {exc})")
            continue
        pages = [PageText(p.page_number, normalize_text(p.text)) for p in raw_pages]
        confidence, needs_ocr = score_confidence(pages, name)
        log.append(f"{name} {version}: fallback, confidence {confidence}")
        if not needs_ocr:
            return ExtractionOutput(
                backend=name,
                backend_version=version,
                pages=pages,
                confidence=confidence,
                needs_ocr=needs_ocr,
                log=[],
            )
    return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def extract_document(
    conn: sqlite3.Connection,
    settings: Settings,
    document_id: int,
    *,
    preferred_backend: str | None = None,
    try_alternates: bool = True,
    actor: str = audit.SYSTEM_ACTOR,
) -> int:
    """Extract and persist text for an archived document. Returns extraction id."""
    row = conn.execute(
        "SELECT stored_path, discovered_id FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no document with id {document_id}")

    path = absolute_path(settings, row["stored_path"])
    output = extract_file(path, preferred_backend=preferred_backend, try_alternates=try_alternates)

    cur = conn.execute(
        """
        INSERT INTO extractions
            (document_id, backend, backend_version, page_count, char_count,
             confidence, needs_ocr, log, extracted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            output.backend,
            output.backend_version,
            output.page_count,
            output.char_count,
            output.confidence,
            1 if output.needs_ocr else 0,
            "\n".join(output.log),
            utcnow(),
        ),
    )
    extraction_id = int(cur.lastrowid)

    conn.executemany(
        """
        INSERT INTO extraction_pages (extraction_id, page_number, text, char_count)
        VALUES (?, ?, ?, ?)
        """,
        [(extraction_id, p.page_number, p.text, p.char_count) for p in output.pages],
    )

    audit.record(
        conn,
        action="extraction.completed",
        entity_type="extraction",
        entity_id=extraction_id,
        actor=actor,
        detail={
            "document_id": document_id,
            "backend": output.backend,
            "pages": output.page_count,
            "chars": output.char_count,
            "confidence": output.confidence,
            "needs_ocr": output.needs_ocr,
        },
    )
    return extraction_id


def load_pages(conn: sqlite3.Connection, extraction_id: int) -> list[PageText]:
    rows = conn.execute(
        "SELECT page_number, text FROM extraction_pages WHERE extraction_id = ? ORDER BY page_number",
        (extraction_id,),
    ).fetchall()
    return [PageText(int(r["page_number"]), r["text"]) for r in rows]
