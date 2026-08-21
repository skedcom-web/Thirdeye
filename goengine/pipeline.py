"""End-to-end pipeline orchestration.

Official Source -> Crawler -> Downloader -> Repository -> Text Extraction
-> Metadata Extraction -> Validation Workbench -> Verified GO Database

Each stage advances the document's status and is independently re-runnable.
A failure in one document never aborts the batch: it is recorded and the run
continues, because a single malformed PDF must not stall a nightly crawl.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import acquisition, audit, registry, repository
from .certification import categorize
from .config import Settings
from .discovery import crawler
from .extraction import metadata as meta
from .extraction import ocr as ocrengine
from .extraction import text as textengine
from .fetching import Fetcher


@dataclass
class StageReport:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def note_failure(self, message: str) -> None:
        self.failed += 1
        self.errors.append(message)


@dataclass
class PipelineReport:
    crawl: list[crawler.CrawlResult] = field(default_factory=list)
    download: StageReport = field(default_factory=StageReport)
    parse: StageReport = field(default_factory=StageReport)

    @property
    def new_documents(self) -> int:
        return sum(r.new_documents for r in self.crawl)


def run_discovery(
    conn: sqlite3.Connection,
    fetcher: Fetcher,
    *,
    only_due: bool = True,
    source_id: int | None = None,
    max_pages: int = 5,
) -> list[crawler.CrawlResult]:
    return crawler.crawl_all(
        conn, fetcher, only_due=only_due, source_id=source_id, max_pages=max_pages
    )


def run_downloads(
    conn: sqlite3.Connection, settings: Settings, fetcher: Fetcher, *,
    limit: int = 50, source_id: int | None = None,
) -> StageReport:
    report = StageReport()
    for result in acquisition.acquire_pending(conn, settings, fetcher, limit=limit, source_id=source_id):
        report.processed += 1
        if result.ok:
            report.succeeded += 1
        else:
            report.note_failure(f"{result.url}: {result.error}")
    return report


def parse_document(
    conn: sqlite3.Connection,
    settings: Settings,
    document_id: int,
    *,
    preferred_backend: str | None = None,
    enable_ocr: bool = True,
    precomputed_ocr: ocrengine.OcrOutput | None = None,
    actor: str = audit.SYSTEM_ACTOR,
) -> int:
    """Text + OCR + metadata extraction for one archived document.

    OCR (Module 4) runs between text extraction and metadata extraction,
    exactly the blueprint's PDF -> Text Detection -> OCR Required? -> OCR
    Engine -> Text Output pipeline: it only touches pages the digital text
    layer left sparse, and never runs at all if Tesseract isn't installed
    (a document just stays flagged `needs_ocr` for later reprocessing).

    `precomputed_ocr` (Phase 3.4): when a local extraction agent already ran
    OCR on hardware that has Tesseract (this server may not -- see the
    module-level note below), its results are merged here via
    `apply_precomputed_ocr` instead of attempting a live `apply_to_extraction`
    call. Only the OCR page *text* crosses this trust boundary; the
    categorization and field-extraction steps below always run from this
    server's own code on the merged text, exactly as for any other document,
    so evidence offsets are never trusted from outside this process.
    Returns the GO record id.
    """
    extraction_id = textengine.extract_document(
        conn, settings, document_id, preferred_backend=preferred_backend,
        # Precomputed OCR is about to override this result regardless of
        # which backend produced it, so there's no point paying for a
        # multi-backend quality hunt -- see extract_file's docstring. This
        # is what made a 55-page scanned GO take three full parses server-
        # side and time out; a real production sync exposed it.
        try_alternates=precomputed_ocr is None,
    )

    if precomputed_ocr is not None:
        ocrengine.apply_precomputed_ocr(conn, extraction_id, precomputed_ocr, actor=actor)
    elif enable_ocr:
        row = conn.execute(
            "SELECT stored_path FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is not None:
            document_path = repository.absolute_path(settings, row["stored_path"])
            try:
                ocrengine.apply_to_extraction(conn, extraction_id, document_path, actor=actor)
            except ocrengine.OcrError as exc:
                audit.record(
                    conn,
                    action="extraction.ocr_failed",
                    entity_type="extraction",
                    entity_id=extraction_id,
                    detail={"error": str(exc)},
                )

    # Language/category identification (Module 2 + 5) runs on the final
    # merged text -- digital or OCR'd, whichever applies per page -- and
    # completes before metadata extraction, per the governance rule that
    # language must be identified before extraction begins.
    categorize.categorize_document(
        conn, document_id, extraction_id, textengine.load_pages(conn, extraction_id)
    )

    record_id = meta.extract_and_store(conn, extraction_id)

    row = conn.execute(
        "SELECT discovered_id FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is not None:
        crawler.set_status(conn, int(row["discovered_id"]), crawler.STATUS_PARSED)
    return record_id


def run_parsing(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    limit: int = 50,
    preferred_backend: str | None = None,
    reparse: bool = False,
    source_id: int | None = None,
) -> StageReport:
    """Parse archived documents that have no GO record yet."""
    source_filter = "AND d.source_id = ?" if source_id is not None else ""
    sql = f"""
        SELECT d.id FROM documents d
         WHERE NOT EXISTS (
                SELECT 1 FROM go_records r
                 WHERE r.document_id = d.id AND r.extractor_version = ?
             )
             {source_filter}
         ORDER BY d.id
         LIMIT ?
    """
    params: list = [meta.EXTRACTOR_VERSION]
    if source_id is not None:
        params.append(source_id)
    params.append(limit)
    if reparse:
        source_filter2 = "WHERE source_id = ?" if source_id is not None else ""
        sql = f"SELECT id FROM documents {source_filter2} ORDER BY id LIMIT ?"
        params = [source_id, limit] if source_id is not None else [limit]

    report = StageReport()
    for row in conn.execute(sql, params).fetchall():
        document_id = int(row["id"])
        report.processed += 1
        try:
            parse_document(
                conn, settings, document_id, preferred_backend=preferred_backend
            )
            report.succeeded += 1
        except (textengine.ExtractionError, LookupError, sqlite3.Error) as exc:
            report.note_failure(f"document {document_id}: {exc}")
            audit.record(
                conn,
                action="parse.failed",
                entity_type="document",
                entity_id=document_id,
                detail={"error": str(exc)},
            )
    return report


def run_all(
    conn: sqlite3.Connection,
    settings: Settings,
    fetcher: Fetcher,
    *,
    only_due: bool = True,
    source_id: int | None = None,
    max_pages: int = 5,
    limit: int = 50,
) -> PipelineReport:
    # source_id now scopes every stage, not just discovery -- a caller
    # asking to run one specific source must never have its download/parse
    # phases silently pick up another source's backlog instead. That bug
    # was confirmed live: a Local Agent request scoped to "Public Works"
    # kept downloading "Health" department leftovers because only crawling
    # was ever source-scoped here.
    report = PipelineReport()
    report.crawl = run_discovery(
        conn, fetcher, only_due=only_due, source_id=source_id, max_pages=max_pages
    )
    report.download = run_downloads(conn, settings, fetcher, limit=limit, source_id=source_id)
    report.parse = run_parsing(conn, settings, limit=limit, source_id=source_id)
    return report


def ingest_document_bytes(
    conn: sqlite3.Connection,
    settings: Settings,
    payload: bytes,
    *,
    source_id: int,
    source_url: str,
    file_name: str,
    link_text: str = "",
    precomputed_ocr: "ocrengine.OcrOutput | None" = None,
    actor: str = audit.SYSTEM_ACTOR,
) -> tuple[int, int, bool]:
    """Archive and parse a PDF already in memory. Returns (document_id,
    go_record_id, is_new_version).

    The bytes-accepting core shared by `ingest_local_file` (CLI `thirdeye
    ingest`, a file on disk) and the Phase 3.4 agent sync endpoint (bytes
    from an HTTP upload) -- the discovered_documents upsert + archive +
    parse sequence exists in exactly one place.
    """
    registry.assert_approved(source_url)
    if registry.get_source(conn, source_id) is None:
        raise LookupError(f"no source with id {source_id}")

    if not acquisition.looks_like_pdf(payload, "application/pdf"):
        raise ValueError(f"{file_name}: not a PDF")

    now_row = conn.execute(
        "SELECT id FROM discovered_documents WHERE source_id = ? AND url = ?",
        (source_id, source_url),
    ).fetchone()
    if now_row is None:
        from .db import utcnow

        cur = conn.execute(
            """
            INSERT INTO discovered_documents
                (source_id, url, link_text, found_on_url, discovered_at, last_seen_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'new')
            """,
            (source_id, source_url, link_text, "manual-ingest", utcnow(), utcnow()),
        )
        discovered_id = int(cur.lastrowid)
        audit.record(
            conn,
            action="document.discovered",
            entity_type="discovered_document",
            entity_id=discovered_id,
            after_value=source_url,
            detail={"source_id": source_id, "found_on": "manual-ingest"},
        )
    else:
        discovered_id = int(now_row["id"])

    stored = repository.store(settings, payload)
    document_id, is_new_version = repository.record_document(
        conn,
        discovered_id=discovered_id,
        source_id=source_id,
        source_url=source_url,
        file_name=file_name,
        stored=stored,
        content_type="application/pdf",
        http_status=None,
        actor=actor,
    )
    crawler.set_status(conn, discovered_id, crawler.STATUS_DOWNLOADED)

    if is_new_version:
        record_id = parse_document(conn, settings, document_id, precomputed_ocr=precomputed_ocr, actor=actor)
    else:
        # Byte-identical resync (e.g. a local agent retrying a sync that
        # already succeeded): this document_id has already been through
        # extract_document/parse_document once. Re-running it would
        # normally just create a brand-new extractions + go_records row for
        # the exact same content -- real duplicate "pending review" rows
        # for one underlying PDF, which is exactly what repeatedly
        # retrying a sync during a recovery did to the review queue before
        # this guard existed.
        #
        # But "same file bytes" is not the same question as "nothing new to
        # contribute": a document that failed OCR the first time (no local
        # agent OCR data available yet) still has needs_ocr=1 and no usable
        # fields. If *this* sync attempt brings real precomputed OCR text
        # where the first one didn't, that's a genuine correction, not a
        # wasted retry -- confirmed live: resyncing a document after fixing
        # its local OCR gap silently kept the original empty-text record
        # otherwise, because "reuse the existing record" doesn't distinguish
        # "already correct" from "still broken, and now fixable."
        existing = conn.execute(
            """
            SELECT r.id AS record_id, r.status, e.needs_ocr
              FROM go_records r JOIN extractions e ON e.id = r.extraction_id
             WHERE r.document_id = ? ORDER BY r.id DESC LIMIT 1
            """,
            (document_id,),
        ).fetchone()
        can_recover = (
            existing is not None
            and existing["status"] == "pending"
            and existing["needs_ocr"]
            and precomputed_ocr is not None
        )
        if existing is None:
            record_id = parse_document(conn, settings, document_id, precomputed_ocr=precomputed_ocr, actor=actor)
        elif can_recover:
            stale_record_id = int(existing["record_id"])
            record_id = parse_document(conn, settings, document_id, precomputed_ocr=precomputed_ocr, actor=actor)
            if record_id != stale_record_id:
                from .operations.dedup import delete_record_and_its_extraction

                delete_record_and_its_extraction(conn, stale_record_id)
        else:
            record_id = int(existing["record_id"])
    return document_id, record_id, is_new_version


def ingest_local_file(
    conn: sqlite3.Connection,
    settings: Settings,
    path: Path,
    *,
    source_id: int,
    source_url: str,
    link_text: str = "",
) -> int:
    """Archive and parse a PDF already on disk. Returns the GO record id.

    For back-filling documents obtained outside the crawler -- an RTI response,
    a manual download. The declared source URL is still policy-checked and
    still recorded, so provenance is no weaker than the crawled path.
    """
    _, record_id, _ = ingest_document_bytes(
        conn, settings, path.read_bytes(),
        source_id=source_id, source_url=source_url, link_text=link_text,
        file_name=acquisition.derive_file_name(source_url, link_text) if link_text else path.name,
    )
    return record_id
