"""Module 4 -- OCR Intelligence Engine.

    PDF -> Text Detection -> OCR Required? -> OCR Engine -> Text Output

"Text Detection" and the "OCR Required?" decision are Module 5 of Phase 1
(`text.py`'s `score_confidence`/`needs_ocr`) -- this module is purely the OCR
engine and the merge step that turns its output into usable page text.

Tesseract is used with English and Tamil trained data (Tamil OCR is a
Phase 2 mandatory requirement). OCR runs per weak PAGE, not per document: a
digitally-typed cover page next to a scanned annexure is common in real GOs,
and re-OCRing pages that already extracted cleanly would only add noise.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import audit
from ..config import OCR_DPI, OCR_LANGUAGES, OCR_MIN_CONFIDENCE, TESSDATA_DIR, TESSERACT_CMD
from ..db import utcnow
from .text import MIN_CHARS_PER_PAGE_FOR_TEXT, PageText, normalize_text, score_confidence

ENGINE_NAME = "tesseract"


class OcrError(RuntimeError):
    pass


@dataclass
class OcrPageResult:
    page_number: int
    text: str
    mean_confidence: float  # 0..1; 0.0 if no words detected

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class OcrOutput:
    engine: str
    engine_version: str
    languages: str
    pages: list[OcrPageResult] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    @property
    def mean_confidence(self) -> float:
        if not self.pages:
            return 0.0
        return sum(p.mean_confidence for p in self.pages) / len(self.pages)


_configured = False


def _configure() -> None:
    """Point pytesseract at the resolved binary and tessdata directory.

    Idempotent, called lazily so importing this module never requires
    Tesseract to be installed. TESSDATA_PREFIX (an env var, not a CLI flag)
    is how tesseract.exe itself is told where the language files live --
    passing "--tessdata-dir" as a pytesseract `config` string instead breaks
    on Windows, since subprocess argv isn't shell-quoted and the literal
    quote characters end up appended to the path.
    """
    global _configured
    if _configured:
        return
    import os as _os

    import pytesseract

    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    if TESSDATA_DIR.is_dir():
        _os.environ["TESSDATA_PREFIX"] = str(TESSDATA_DIR)
    _configured = True


def is_available() -> bool:
    """True if the OCR engine can actually run: binary present, at least the
    English language pack resolvable via TESSDATA_DIR or the binary's own
    tessdata."""
    if not TESSERACT_CMD or not Path(TESSERACT_CMD).exists():
        return False
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return True


def available_languages() -> list[str]:
    if TESSDATA_DIR.is_dir():
        local = sorted(p.stem for p in TESSDATA_DIR.glob("*.traineddata"))
        if local:
            return local
    if not is_available():
        return []
    try:
        _configure()
        import pytesseract

        return sorted(pytesseract.get_languages())
    except Exception:
        return []


def engine_version() -> str:
    try:
        _configure()
        import pytesseract

        return str(pytesseract.get_tesseract_version())
    except Exception:
        return "unknown"


def _render_page(path: Path, page_number: int, dpi: int):
    import pymupdf

    with pymupdf.open(path) as doc:
        page = doc[page_number - 1]
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png")


def ocr_page(image_bytes: bytes, *, languages: str = OCR_LANGUAGES) -> tuple[str, float]:
    """OCR one page image. Returns (text, mean word confidence 0..1)."""
    _configure()
    import io

    import pytesseract
    from PIL import Image

    image = Image.open(io.BytesIO(image_bytes))
    text = pytesseract.image_to_string(image, lang=languages)
    data = pytesseract.image_to_data(image, lang=languages, output_type=pytesseract.Output.DICT)
    confidences = [int(c) for c in data.get("conf", []) if str(c) not in ("-1", "")]
    mean_confidence = (sum(confidences) / len(confidences) / 100.0) if confidences else 0.0
    return normalize_text(text), round(mean_confidence, 4)


def ocr_pdf(
    path: Path,
    page_numbers: list[int],
    *,
    languages: str = OCR_LANGUAGES,
    dpi: int = OCR_DPI,
) -> OcrOutput:
    """Run OCR on the given 1-based page numbers of a PDF."""
    if not is_available():
        raise OcrError(
            "Tesseract is not available: install it and set THIRDEYE_TESSERACT_CMD "
            "if it is not on PATH, and place eng.traineddata/tam.traineddata in "
            f"{TESSDATA_DIR}"
        )

    output = OcrOutput(engine=ENGINE_NAME, engine_version=engine_version(), languages=languages)
    for page_number in page_numbers:
        try:
            image_bytes = _render_page(path, page_number, dpi)
            text, confidence = ocr_page(image_bytes, languages=languages)
        except Exception as exc:
            output.log.append(f"page {page_number}: OCR failed ({type(exc).__name__}: {exc})")
            output.pages.append(OcrPageResult(page_number, "", 0.0))
            continue
        output.log.append(
            f"page {page_number}: {len(text)} chars, confidence {confidence:.2f}"
        )
        output.pages.append(OcrPageResult(page_number, text, confidence))
    return output


# ---------------------------------------------------------------------------
# Persistence / merge into the extraction record
# ---------------------------------------------------------------------------
def apply_to_extraction(
    conn: sqlite3.Connection,
    extraction_id: int,
    document_path: Path,
    *,
    languages: str = OCR_LANGUAGES,
    dpi: int = OCR_DPI,
    min_confidence: float = OCR_MIN_CONFIDENCE,
    actor: str = audit.SYSTEM_ACTOR,
) -> OcrOutput | None:
    """OCR the weak pages of an existing extraction and merge in improvements.

    Returns None if there was nothing worth OCRing (e.g. Tesseract missing).
    """
    weak_pages = _weak_pages(conn, extraction_id)
    if not weak_pages:
        return None
    if not is_available():
        audit.record(
            conn,
            action="extraction.ocr_skipped",
            entity_type="extraction",
            entity_id=extraction_id,
            actor=actor,
            detail={"reason": "Tesseract not available", "weak_pages": weak_pages},
        )
        return None

    output = ocr_pdf(document_path, weak_pages, languages=languages, dpi=dpi)
    _merge_ocr_output(conn, extraction_id, output, min_confidence=min_confidence, actor=actor, source="server")
    return output


def apply_precomputed_ocr(
    conn: sqlite3.Connection,
    extraction_id: int,
    output: OcrOutput,
    *,
    min_confidence: float = OCR_MIN_CONFIDENCE,
    actor: str = audit.SYSTEM_ACTOR,
) -> None:
    """Merges OCR results a local agent already computed (Tesseract may not
    be installed on this machine at all -- see the Phase 3.4 design note in
    goengine/pipeline.py -- so this never calls ocr_pdf()/is_available()).
    Only the OCR page text crosses the trust boundary; everything downstream
    (categorization, field extraction) still runs from this server's own
    code on the merged text, exactly as for any other document."""
    if not output.pages:
        return
    _merge_ocr_output(conn, extraction_id, output, min_confidence=min_confidence, actor=actor, source="local_agent")


def _weak_pages(conn: sqlite3.Connection, extraction_id: int) -> list[int]:
    pages = conn.execute(
        "SELECT page_number, char_count FROM extraction_pages WHERE extraction_id = ?",
        (extraction_id,),
    ).fetchall()
    return [int(r["page_number"]) for r in pages if int(r["char_count"]) < MIN_CHARS_PER_PAGE_FOR_TEXT]


def _merge_ocr_output(
    conn: sqlite3.Connection,
    extraction_id: int,
    output: OcrOutput,
    *,
    min_confidence: float,
    actor: str,
    source: str,
) -> None:
    """Records an OCR run and merges its results into `extraction_pages`.

    A page's digital text is replaced only when the OCR result is both longer
    and above `min_confidence` -- otherwise the sparse digital text is kept
    rather than risk seeding field extraction with low-confidence OCR noise.
    `source` ('server' | 'local_agent') records whether this ran live on this
    machine or was submitted pre-computed by a local extraction agent."""
    pages = conn.execute(
        "SELECT page_number, text, char_count FROM extraction_pages WHERE extraction_id = ?",
        (extraction_id,),
    ).fetchall()
    weak_pages = [int(r["page_number"]) for r in pages if int(r["char_count"]) < MIN_CHARS_PER_PAGE_FOR_TEXT]

    ran_at = utcnow()
    cur = conn.execute(
        """
        INSERT INTO ocr_runs
            (extraction_id, engine, engine_version, languages, pages_ocred,
             mean_word_confidence, ran_at, log, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            extraction_id, output.engine, output.engine_version, output.languages,
            len(output.pages), output.mean_confidence, ran_at, "\n".join(output.log), source,
        ),
    )
    ocr_run_id = int(cur.lastrowid)
    conn.executemany(
        """
        INSERT INTO ocr_pages (ocr_run_id, page_number, text, mean_word_confidence)
        VALUES (?, ?, ?, ?)
        """,
        [(ocr_run_id, p.page_number, p.text, p.mean_confidence) for p in output.pages],
    )

    existing_by_page = {int(r["page_number"]): r for r in pages}
    improved_pages: list[int] = []
    for page_result in output.pages:
        existing = existing_by_page.get(page_result.page_number)
        if existing is None:
            continue
        if page_result.char_count > int(existing["char_count"]) and page_result.mean_confidence >= min_confidence:
            conn.execute(
                """
                UPDATE extraction_pages
                   SET text = ?, char_count = ?, source = 'ocr'
                 WHERE extraction_id = ? AND page_number = ?
                """,
                (page_result.text, page_result.char_count, extraction_id, page_result.page_number),
            )
            improved_pages.append(page_result.page_number)

    if improved_pages:
        all_rows = conn.execute(
            "SELECT page_number, text, char_count FROM extraction_pages WHERE extraction_id = ?",
            (extraction_id,),
        ).fetchall()
        still_weak = sum(1 for r in all_rows if int(r["char_count"]) < MIN_CHARS_PER_PAGE_FOR_TEXT)
        new_char_total = sum(int(r["char_count"]) for r in all_rows)

        # Re-score the merged document like any other backend, then discount
        # by the OCR's own confidence -- clean OCR text still deserves less
        # trust than a real digital text layer for the pages it replaced.
        final_pages = [PageText(int(r["page_number"]), r["text"]) for r in all_rows]
        base_confidence, _ = score_confidence(final_pages, backend="pdfplumber")
        ocr_confidences = [p.mean_confidence for p in output.pages if p.page_number in improved_pages]
        ocr_factor = sum(ocr_confidences) / len(ocr_confidences) if ocr_confidences else 0.0
        blended_confidence = round(base_confidence * (0.5 + 0.5 * ocr_factor), 4)

        conn.execute(
            """
            UPDATE extractions
               SET ocr_applied = 1, ocr_pages_count = ?, char_count = ?,
                   needs_ocr = ?, confidence = ?
             WHERE id = ?
            """,
            (len(improved_pages), new_char_total, 1 if still_weak else 0, blended_confidence, extraction_id),
        )

    audit.record(
        conn,
        action="extraction.ocr_applied",
        entity_type="extraction",
        entity_id=extraction_id,
        actor=actor,
        detail={
            "ocr_run_id": ocr_run_id,
            "weak_pages": weak_pages,
            "improved_pages": improved_pages,
            "languages": output.languages,
            "mean_confidence": output.mean_confidence,
            "source": source,
        },
    )
