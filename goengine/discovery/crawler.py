"""Module 2 -- Source Discovery Engine.

Crawls approved sources, records every document URL exactly once, and tracks
the status lifecycle: new -> downloaded -> parsed -> verified | rejected.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .. import audit, registry
from ..db import utcnow
from ..fetching import Fetcher, FetchError
from .adapters import get_adapter

STATUS_NEW = "new"
STATUS_DOWNLOADED = "downloaded"
STATUS_PARSED = "parsed"
STATUS_VERIFIED = "verified"
STATUS_REJECTED = "rejected"

ALL_STATUSES = (STATUS_NEW, STATUS_DOWNLOADED, STATUS_PARSED, STATUS_VERIFIED, STATUS_REJECTED)

FREQUENCY_INTERVALS = {
    "hourly": timedelta(hours=1),
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
}


@dataclass
class CrawlResult:
    source_id: int
    source_name: str
    run_id: int
    status: str
    pages_fetched: int = 0
    links_seen: int = 0
    new_documents: int = 0
    duplicate_documents: int = 0
    error: str | None = None
    new_urls: list[str] = field(default_factory=list)


def is_due(source: registry.Source, *, now: datetime | None = None) -> bool:
    """Change detection at the source level: has the crawl interval elapsed?"""
    if not source.active:
        return False
    if source.crawl_frequency == "manual":
        return False
    if not source.last_crawl_at:
        return True
    interval = FREQUENCY_INTERVALS.get(source.crawl_frequency)
    if interval is None:
        return True
    try:
        last = datetime.fromisoformat(source.last_crawl_at)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - last >= interval


def crawl_source(
    conn: sqlite3.Connection,
    fetcher: Fetcher,
    source: registry.Source,
    *,
    max_pages: int = 5,
    actor: str = audit.SYSTEM_ACTOR,
) -> CrawlResult:
    """Crawl one source and register every document URL found."""
    cur = conn.execute(
        "INSERT INTO crawl_runs (source_id, started_at, status) VALUES (?, ?, 'running')",
        (source.id, utcnow()),
    )
    run_id = int(cur.lastrowid)
    result = CrawlResult(
        source_id=source.id, source_name=source.name, run_id=run_id, status="ok"
    )

    audit.record(
        conn,
        action="crawl.started",
        entity_type="source",
        entity_id=source.id,
        actor=actor,
        detail={"run_id": run_id, "url": source.url, "adapter": source.adapter},
    )

    # Initialize dynamic telemetry counters
    pages_visited = 0
    dept_pages_found = 0
    go_listings_found = 0
    doc_pages_found = 0
    doc_links_found = 0
    pdf_links_found = 0
    downloaded_count = 0
    parsed_count = 0
    ocr_count = 0
    parser_failures = 0
    download_failures = 0
    rejected_links = 0
    skipped_links = 0
    proxy_used = ""
    ssl_fallback_used = 0
    user_agent = ""

    try:
        adapter = get_adapter(source.adapter)
        queue: list[str] = [source.url]
        visited: set[str] = set()

        ssl_verify = bool(source.ssl_verification_enabled)
        ssl_fallback = bool(source.allow_ssl_fallback)

        while queue and result.pages_fetched < max_pages:
            page_url = queue.pop(0)
            if page_url in visited:
                continue
            visited.add(page_url)

            response = None
            fetch_error_msg = None
            try:
                response = fetcher.get(page_url, verify=ssl_verify, allow_fallback=ssl_fallback)
                proxy_used = response.proxy_used or ""
                user_agent = response.user_agent or ""
                if not response.ssl_verified and ssl_fallback:
                    ssl_fallback_used = 1
            except FetchError as exc:
                fetch_error_msg = str(exc)
                response = exc.response
                if response:
                    proxy_used = response.proxy_used or ""
                    user_agent = response.user_agent or ""
                    if not response.ssl_verified and ssl_fallback:
                        ssl_fallback_used = 1

            # Log request-level evidence
            status_code = response.status_code if response else 0
            response_size = len(response.content) if (response and response.content) else 0
            content_type = response.content_type if response else "unknown"
            response_time_ms = response.response_time_ms if response else 0.0
            duration_ms = response.duration_ms if response else 0.0
            redirect_count = response.redirect_count if response else 0
            ssl_verified_num = 1 if (response and response.ssl_verified) else 0
            failure_category = response.failure_category if (response and response.failure_category) else ""
            failure_subtype = response.failure_subtype if (response and response.failure_subtype) else ""
            err_msg = response.error_message if (response and response.error_message) else (fetch_error_msg or "")
            last_successful_stage = response.last_successful_stage if response else None
            failure_stage = response.failure_stage if response else None
            confidence_level = response.confidence_level if response else ""
            exception_type = response.exception_type if response else ""
            exception_message = response.exception_message if response else ""

            if fetch_error_msg and not failure_category:
                # HttpFetcher always attaches a classified Response now (see
                # network_diagnosis.py); reaching this branch means the
                # exception came from somewhere the classifier hasn't been
                # taught about yet -- UNKNOWN_FAILURE, not the old
                # "network_failure" default, is itself useful signal that
                # the classifier needs extending, not a resigned guess.
                from ..operations import network_diagnosis as diag
                failure_category = diag.UNKNOWN_FAILURE
                failure_subtype = diag.UNKNOWN_FAILURE
                confidence_level = diag.CONFIDENCE_LOW

            conn.execute(
                """
                INSERT INTO crawl_evidences
                    (crawl_run_id, url, status_code, response_size, content_type,
                     response_time_ms, duration_ms, redirect_count, timestamp,
                     user_agent, proxy_used, ssl_verified, error_message,
                     failure_category, failure_subtype,
                     last_successful_stage, failure_stage, confidence_level,
                     exception_type, exception_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, page_url, status_code, response_size, content_type,
                    response_time_ms, duration_ms, redirect_count, utcnow(),
                    user_agent, proxy_used, ssl_verified_num, err_msg,
                    failure_category, failure_subtype,
                    last_successful_stage, failure_stage, confidence_level,
                    exception_type, exception_message
                )
            )

            if fetch_error_msg:
                # Re-raise the fetch error to register crawl.failed and update state
                raise FetchError(fetch_error_msg, response)

            result.pages_fetched += 1
            pages_visited += 1

            if response.status_code != 200:
                audit.record(
                    conn,
                    action="crawl.page_error",
                    entity_type="source",
                    entity_id=source.id,
                    actor=actor,
                    detail={"url": page_url, "status": response.status_code},
                )
                continue

            page = adapter.parse(response.text, response.url)
            result.links_seen += len(page.documents)

            # Accumulate adapter metrics
            dept_pages_found += page.dept_pages_found
            go_listings_found += page.go_listings_found
            doc_pages_found += page.doc_pages_found
            doc_links_found += page.doc_links_found
            pdf_links_found += page.pdf_links_found
            rejected_links += page.rejected_links
            skipped_links += page.skipped_links

            for link in page.documents:
                created = _register_link(
                    conn,
                    source_id=source.id,
                    url=link.url,
                    link_text=link.link_text,
                    found_on_url=link.found_on_url or page_url,
                    run_id=run_id,
                    hints=link.hints,
                    actor=actor,
                )
                if created:
                    result.new_documents += 1
                    result.new_urls.append(link.url)
                else:
                    result.duplicate_documents += 1
                    skipped_links += 1

            for follow_url in page.follow:
                if follow_url not in visited:
                    queue.append(follow_url)

    except (FetchError, LookupError) as exc:
        result.status = "error"
        result.error = str(exc)
        audit.record(
            conn,
            action="crawl.failed",
            entity_type="source",
            entity_id=source.id,
            actor=actor,
            detail={"run_id": run_id, "error": str(exc)},
        )

    conn.execute(
        """
        UPDATE crawl_runs
           SET finished_at = ?, status = ?, pages_fetched = ?, links_seen = ?,
               new_documents = ?, duplicate_documents = ?, error = ?,
               pages_visited = ?, dept_pages_found = ?, go_listings_found = ?,
               doc_pages_found = ?, doc_links_found = ?, pdf_links_found = ?,
               downloaded_count = ?, parsed_count = ?, ocr_count = ?,
               parser_failures = ?, download_failures = ?, rejected_links = ?,
               skipped_links = ?, proxy_used = ?, ssl_fallback_used = ?,
               user_agent = ?
         WHERE id = ?
        """,
        (
            utcnow(),
            result.status,
            result.pages_fetched,
            result.links_seen,
            result.new_documents,
            result.duplicate_documents,
            result.error,
            pages_visited,
            dept_pages_found,
            go_listings_found,
            doc_pages_found,
            doc_links_found,
            pdf_links_found,
            downloaded_count,
            parsed_count,
            ocr_count,
            parser_failures,
            download_failures,
            rejected_links,
            skipped_links,
            proxy_used,
            ssl_fallback_used,
            user_agent,
            run_id,
        ),
    )
    registry.mark_crawled(conn, source.id, result.status)

    audit.record(
        conn,
        action="crawl.finished",
        entity_type="source",
        entity_id=source.id,
        actor=actor,
        detail={
            "run_id": run_id,
            "status": result.status,
            "pages_fetched": result.pages_fetched,
            "new_documents": result.new_documents,
            "duplicate_documents": result.duplicate_documents,
        },
    )
    return result


def crawl_all(
    conn: sqlite3.Connection,
    fetcher: Fetcher,
    *,
    only_due: bool = True,
    max_pages: int = 5,
    source_id: int | None = None,
    actor: str = audit.SYSTEM_ACTOR,
) -> list[CrawlResult]:
    if source_id is not None:
        source = registry.get_source(conn, source_id)
        if source is None:
            raise LookupError(f"no source with id {source_id}")
        sources = [source]
    else:
        sources = registry.list_sources(conn, active_only=True)
        if only_due:
            sources = [s for s in sources if is_due(s)]
    return [crawl_source(conn, fetcher, s, max_pages=max_pages, actor=actor) for s in sources]


def _register_link(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    url: str,
    link_text: str,
    found_on_url: str,
    run_id: int,
    hints: dict[str, str],
    actor: str,
) -> bool:
    """Insert a discovered URL. Returns True if it is newly seen.

    Duplicate prevention rests on UNIQUE(source_id, url): a URL already on file
    for this source only has its last_seen_at refreshed, so re-crawling a
    listing page never produces a second copy of the same order.
    """
    now = utcnow()
    existing = conn.execute(
        "SELECT id FROM discovered_documents WHERE source_id = ? AND url = ?",
        (source_id, url),
    ).fetchone()

    if existing is not None:
        conn.execute(
            "UPDATE discovered_documents SET last_seen_at = ? WHERE id = ?",
            (now, int(existing["id"])),
        )
        return False

    cur = conn.execute(
        """
        INSERT INTO discovered_documents
            (source_id, url, link_text, found_on_url, discovered_at,
             first_crawl_run_id, last_seen_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'new')
        """,
        (source_id, url, link_text, found_on_url, now, run_id, now),
    )
    discovered_id = int(cur.lastrowid)

    audit.record(
        conn,
        action="document.discovered",
        entity_type="discovered_document",
        entity_id=discovered_id,
        actor=actor,
        after_value=url,
        detail={
            "source_id": source_id,
            "found_on": found_on_url,
            "link_text": link_text,
            "listing_hints": hints or None,
            "crawl_run_id": run_id,
        },
    )
    return True


def set_status(
    conn: sqlite3.Connection,
    discovered_id: int,
    status: str,
    *,
    reason: str | None = None,
    actor: str = audit.SYSTEM_ACTOR,
) -> None:
    if status not in ALL_STATUSES:
        raise ValueError(f"status must be one of {ALL_STATUSES}")
    row = conn.execute(
        "SELECT status FROM discovered_documents WHERE id = ?", (discovered_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no discovered document with id {discovered_id}")
    before = row["status"]
    if before == status:
        return
    conn.execute(
        "UPDATE discovered_documents SET status = ?, status_reason = ? WHERE id = ?",
        (status, reason, discovered_id),
    )
    audit.record(
        conn,
        action="document.status_changed",
        entity_type="discovered_document",
        entity_id=discovered_id,
        actor=actor,
        field_name="status",
        before_value=before,
        after_value=status,
        detail={"reason": reason} if reason else None,
    )


def pending_downloads(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT d.*, s.name AS source_name, s.department AS source_department
          FROM discovered_documents d
          JOIN sources s ON s.id = d.source_id
         WHERE d.status = 'new'
         ORDER BY d.id
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def counts_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM discovered_documents GROUP BY status"
    ).fetchall()
    counts = {status: 0 for status in ALL_STATUSES}
    for row in rows:
        counts[row["status"]] = int(row["n"])
    return counts
