"""Module 3 -- Document Acquisition Engine.

Downloads original documents from approved sources and hands the unmodified
bytes to the repository. Nothing here rewrites, compresses or normalizes a
file: the archived object must stay byte-identical to what the government
server served.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from . import audit, repository
from .config import Settings
from .discovery import crawler
from .fetching import Fetcher, FetchError
from .registry import SourceRejected, assert_approved

PDF_MAGIC = b"%PDF-"

# "G.O. (Ms) No. 123, dated 2026" and friends, as they appear in link text.
GO_NUMBER_RE = re.compile(
    r"\bG\.?\s*O\.?\s*(?:\(?\s*(?:Ms|MS|Rt|RT|D|P)\s*\)?\.?)?\s*(?:No\.?|Number)?\s*(\d{1,5})",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


@dataclass
class AcquisitionResult:
    discovered_id: int
    url: str
    ok: bool
    document_id: int | None = None
    sha256: str | None = None
    new_version: bool = False
    skipped: bool = False
    error: str | None = None


def derive_file_name(url: str, link_text: str = "") -> str:
    """Human-facing name, e.g. GO-123-2026.pdf.

    Cosmetic only -- the archived path is the SHA256. Falls back to the URL
    basename when the listing gives us nothing to work with.
    """
    haystack = f"{link_text} {unquote(url)}"
    go_match = GO_NUMBER_RE.search(haystack)
    year_match = YEAR_RE.search(haystack)
    if go_match:
        number = go_match.group(1)
        year = year_match.group(0) if year_match else "unknown"
        return f"GO-{number}-{year}.pdf"

    basename = PurePosixPath(unquote(urlparse(url).path)).name
    if basename:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", basename).strip("-")
        if cleaned:
            return cleaned if cleaned.lower().endswith(".pdf") else f"{cleaned}.pdf"
    return "document.pdf"


def looks_like_pdf(payload: bytes, content_type: str) -> bool:
    if payload.startswith(PDF_MAGIC):
        return True
    # Some handlers emit a BOM or whitespace before the header.
    return PDF_MAGIC in payload[:1024] and "pdf" in content_type


def acquire_one(
    conn: sqlite3.Connection,
    settings: Settings,
    fetcher: Fetcher,
    discovered_id: int,
    *,
    actor: str = audit.SYSTEM_ACTOR,
) -> AcquisitionResult:
    row = conn.execute(
        "SELECT * FROM discovered_documents WHERE id = ?", (discovered_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no discovered document with id {discovered_id}")

    url = row["url"]
    result = AcquisitionResult(discovered_id=discovered_id, url=url, ok=False)

    try:
        # Re-check the policy at download time: the registry could have been
        # edited, or the crawl could predate a policy change.
        assert_approved(url)
        response = fetcher.get(url)
    except (SourceRejected, FetchError) as exc:
        result.error = str(exc)
        crawler.set_status(conn, discovered_id, crawler.STATUS_REJECTED, reason=str(exc), actor=actor)
        audit.record(
            conn,
            action="document.download_failed",
            entity_type="discovered_document",
            entity_id=discovered_id,
            actor=actor,
            detail={"url": url, "error": str(exc)},
        )
        return result

    if response.status_code != 200:
        result.error = f"HTTP {response.status_code}"
        audit.record(
            conn,
            action="document.download_failed",
            entity_type="discovered_document",
            entity_id=discovered_id,
            actor=actor,
            detail={"url": url, "status": response.status_code},
        )
        return result

    if not looks_like_pdf(response.content, response.content_type):
        result.error = f"not a PDF (content-type {response.content_type or 'unknown'})"
        crawler.set_status(
            conn, discovered_id, crawler.STATUS_REJECTED, reason=result.error, actor=actor
        )
        audit.record(
            conn,
            action="document.rejected",
            entity_type="discovered_document",
            entity_id=discovered_id,
            actor=actor,
            detail={"url": url, "reason": result.error},
        )
        return result

    stored = repository.store(settings, response.content)
    document_id, is_new = repository.record_document(
        conn,
        discovered_id=discovered_id,
        source_id=int(row["source_id"]),
        source_url=response.url,
        file_name=derive_file_name(url, row["link_text"] or ""),
        stored=stored,
        content_type=response.content_type or None,
        http_status=response.status_code,
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
        actor=actor,
    )

    crawler.set_status(conn, discovered_id, crawler.STATUS_DOWNLOADED, actor=actor)

    result.ok = True
    result.document_id = document_id
    result.sha256 = stored.sha256
    result.new_version = is_new
    result.skipped = not is_new
    return result


def acquire_pending(
    conn: sqlite3.Connection,
    settings: Settings,
    fetcher: Fetcher,
    *,
    limit: int = 50,
    actor: str = audit.SYSTEM_ACTOR,
) -> list[AcquisitionResult]:
    rows = crawler.pending_downloads(conn, limit=limit)
    return [acquire_one(conn, settings, fetcher, int(r["id"]), actor=actor) for r in rows]


def redownload(
    conn: sqlite3.Connection,
    settings: Settings,
    fetcher: Fetcher,
    discovered_id: int,
    *,
    actor: str = audit.SYSTEM_ACTOR,
) -> AcquisitionResult:
    """Re-fetch an already-archived URL to detect a changed source document.

    A different SHA256 produces a new version; the earlier bytes stay put.
    """
    return acquire_one(conn, settings, fetcher, discovered_id, actor=actor)
