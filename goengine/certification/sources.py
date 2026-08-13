"""Module 1 -- Source Certification Engine.

Runs five checks against a live official source and produces one of three
certification results. Certification exercises the *real* pipeline
components (the same crawler and acquisition code the nightly run uses)
rather than a separate ad hoc HTTP probe, so a pass here is evidence the
production path actually works against this source.

    CERTIFIED            all five checks pass
    PARTIALLY_CERTIFIED  reachable and genuine, but discovery/download/
                          stability did not fully succeed this run
    FAILED               unreachable, or not a genuine official host,
                          or discovery found nothing at all
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field

from .. import acquisition, audit, registry
from ..config import Settings
from ..db import utcnow
from ..discovery import crawler
from ..fetching import Fetcher, FetchError

RESULT_CERTIFIED = "CERTIFIED"
RESULT_PARTIAL = "PARTIALLY_CERTIFIED"
RESULT_FAILED = "FAILED"

# Historical crawl success rate below this, with enough history to judge,
# counts against stability even if the immediate repeat-fetch looked fine.
STABILITY_HISTORY_THRESHOLD = 0.7
STABILITY_MIN_HISTORY = 3
# Two back-to-back fetches of the same URL should return similarly sized
# content; a source that varies wildly between requests is not stable.
STABILITY_SIZE_TOLERANCE = 0.5


@dataclass
class CertificationResult:
    source_id: int
    certification_id: int
    result: str
    connectivity_ok: bool | None = None
    discovery_ok: bool | None = None
    download_ok: bool | None = None
    stability_ok: bool | None = None
    authenticity_ok: bool | None = None
    documents_discovered: int = 0
    documents_downloaded: int = 0
    messages: dict[str, str] = field(default_factory=dict)


def _check_connectivity(fetcher: Fetcher, url: str, messages: dict[str, str]) -> bool:
    try:
        started = time.monotonic()
        response = fetcher.get(url)
        elapsed_ms = round((time.monotonic() - started) * 1000)
    except FetchError as exc:
        messages["connectivity"] = f"unreachable: {exc}"
        return False
    except registry.SourceRejected as exc:
        # fetcher.get() re-checks the allowlist itself before making a
        # request; a URL that has drifted off an approved host fails
        # connectivity here rather than raising out of certify_source.
        messages["connectivity"] = f"host not approved: {exc}"
        return False
    if response.status_code != 200:
        messages["connectivity"] = f"HTTP {response.status_code}"
        return False
    messages["connectivity"] = f"HTTP 200 in {elapsed_ms}ms, {len(response.content)} bytes"
    return True


def _check_authenticity(source: registry.Source, messages: dict[str, str]) -> bool:
    try:
        host = registry.assert_approved(source.url)
    except registry.SourceRejected as exc:
        messages["authenticity"] = f"host no longer approved: {exc}"
        return False
    if host != source.host:
        # The registry snapshot at registration time has drifted from policy.
        messages["authenticity"] = f"host mismatch: registered {source.host!r}, now {host!r}"
        return False
    messages["authenticity"] = f"host {host!r} is an approved government source"
    return True


def _check_stability(
    conn: sqlite3.Connection, fetcher: Fetcher, source: registry.Source, messages: dict[str, str]
) -> bool:
    try:
        first = fetcher.get(source.url)
        second = fetcher.get(source.url)
    except FetchError as exc:
        messages["stability"] = f"repeat fetch failed: {exc}"
        return False
    except registry.SourceRejected as exc:
        messages["stability"] = f"host not approved: {exc}"
        return False

    if first.status_code != second.status_code:
        messages["stability"] = (
            f"inconsistent status codes: {first.status_code} then {second.status_code}"
        )
        return False

    size_a, size_b = len(first.content), len(second.content)
    largest = max(size_a, size_b, 1)
    drift = abs(size_a - size_b) / largest
    repeat_ok = drift <= STABILITY_SIZE_TOLERANCE

    history = conn.execute(
        "SELECT status FROM crawl_runs WHERE source_id = ? ORDER BY id DESC LIMIT 5",
        (source.id,),
    ).fetchall()
    if len(history) >= STABILITY_MIN_HISTORY:
        success_rate = sum(1 for h in history if h["status"] == "ok") / len(history)
        history_ok = success_rate >= STABILITY_HISTORY_THRESHOLD
        messages["stability"] = (
            f"repeat-fetch drift {drift:.0%} ({'ok' if repeat_ok else 'high'}); "
            f"{len(history)}-run history success rate {success_rate:.0%} "
            f"({'ok' if history_ok else 'below threshold'})"
        )
        return repeat_ok and history_ok

    messages["stability"] = f"repeat-fetch drift {drift:.0%}; not enough crawl history yet to judge trend"
    return repeat_ok


def certify_source(
    conn: sqlite3.Connection,
    settings: Settings,
    fetcher: Fetcher,
    source_id: int,
    *,
    max_pages: int = 3,
    download_sample: int = 3,
    actor: str = audit.SYSTEM_ACTOR,
) -> CertificationResult:
    source = registry.get_source(conn, source_id)
    if source is None:
        raise LookupError(f"no source with id {source_id}")

    started_at = utcnow()
    messages: dict[str, str] = {}

    connectivity_ok = _check_connectivity(fetcher, source.url, messages)
    authenticity_ok = _check_authenticity(source, messages)

    discovery_ok = False
    documents_discovered = 0
    if connectivity_ok:
        crawl_result = crawler.crawl_source(conn, fetcher, source, max_pages=max_pages, actor=actor)
        documents_discovered = crawl_result.new_documents + crawl_result.duplicate_documents
        discovery_ok = crawl_result.status == "ok" and documents_discovered > 0
        messages["discovery"] = (
            f"{documents_discovered} document(s) found across {crawl_result.pages_fetched} page(s)"
            if discovery_ok
            else f"crawl status {crawl_result.status}, {documents_discovered} document(s) found"
        )
    else:
        messages["discovery"] = "skipped: source unreachable"

    download_ok = False
    documents_downloaded = 0
    if discovery_ok:
        candidates = crawler.pending_downloads(conn, limit=50)
        candidates = [row for row in candidates if int(row["source_id"]) == source_id][:download_sample]
        valid_pdfs = 0
        for row in candidates:
            result = acquisition.acquire_one(conn, settings, fetcher, int(row["id"]), actor=actor)
            if result.ok:
                documents_downloaded += 1
                valid_pdfs += 1
        download_ok = valid_pdfs > 0 and valid_pdfs == len(candidates) if candidates else False
        messages["download"] = (
            f"{valid_pdfs}/{len(candidates)} sampled document(s) downloaded as valid PDFs"
            if candidates
            else "no undownloaded documents available to sample"
        )
    else:
        messages["download"] = "skipped: discovery did not succeed"

    stability_ok = _check_stability(conn, fetcher, source, messages) if connectivity_ok else False
    if not connectivity_ok:
        messages["stability"] = "skipped: source unreachable"

    checks = {
        "connectivity": connectivity_ok,
        "discovery": discovery_ok,
        "download": download_ok,
        "stability": stability_ok,
        "authenticity": authenticity_ok,
    }
    if not connectivity_ok or not authenticity_ok or not discovery_ok:
        overall = RESULT_FAILED
    elif all(checks.values()):
        overall = RESULT_CERTIFIED
    else:
        overall = RESULT_PARTIAL

    finished_at = utcnow()
    cur = conn.execute(
        """
        INSERT INTO source_certifications
            (source_id, started_at, finished_at, result, connectivity_ok, discovery_ok,
             download_ok, stability_ok, authenticity_ok, documents_discovered,
             documents_downloaded, detail, actor)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id, started_at, finished_at, overall,
            int(connectivity_ok), int(discovery_ok), int(download_ok),
            int(stability_ok), int(authenticity_ok),
            documents_discovered, documents_downloaded,
            json.dumps(messages, ensure_ascii=False), actor,
        ),
    )
    certification_id = int(cur.lastrowid)

    # registry.Source predates Phase 2 and doesn't carry the new columns, so
    # read the prior status directly rather than extending that dataclass.
    before_status = conn.execute(
        "SELECT certification_status FROM sources WHERE id = ?", (source_id,)
    ).fetchone()["certification_status"]
    conn.execute(
        "UPDATE sources SET certification_status = ?, certification_date = ? WHERE id = ?",
        (overall, finished_at, source_id),
    )
    if connectivity_ok:
        conn.execute(
            "UPDATE sources SET last_crawl_success_at = ? WHERE id = ?", (finished_at, source_id)
        )
    else:
        conn.execute(
            "UPDATE sources SET last_crawl_failure_at = ? WHERE id = ?", (finished_at, source_id)
        )

    audit.record(
        conn,
        action="source.certification_run",
        entity_type="source",
        entity_id=source_id,
        actor=actor,
        field_name="certification_status",
        before_value=before_status,
        after_value=overall,
        detail={
            "certification_id": certification_id,
            "checks": checks,
            "documents_discovered": documents_discovered,
            "documents_downloaded": documents_downloaded,
            "messages": messages,
        },
    )

    return CertificationResult(
        source_id=source_id,
        certification_id=certification_id,
        result=overall,
        connectivity_ok=connectivity_ok,
        discovery_ok=discovery_ok,
        download_ok=download_ok,
        stability_ok=stability_ok,
        authenticity_ok=authenticity_ok,
        documents_discovered=documents_discovered,
        documents_downloaded=documents_downloaded,
        messages=messages,
    )


def certify_all(
    conn: sqlite3.Connection,
    settings: Settings,
    fetcher: Fetcher,
    *,
    active_only: bool = True,
    max_pages: int = 3,
    download_sample: int = 3,
    actor: str = audit.SYSTEM_ACTOR,
) -> list[CertificationResult]:
    sources = registry.list_sources(conn, active_only=active_only)
    return [
        certify_source(
            conn, settings, fetcher, s.id,
            max_pages=max_pages, download_sample=download_sample, actor=actor,
        )
        for s in sources
    ]


def certification_summary(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT certification_status, COUNT(*) AS n FROM sources GROUP BY certification_status"
    ).fetchall()
    summary = {RESULT_CERTIFIED: 0, RESULT_PARTIAL: 0, RESULT_FAILED: 0, "PENDING": 0}
    for row in rows:
        summary[row["certification_status"]] = int(row["n"])
    return summary


def certified_count(conn: sqlite3.Connection) -> int:
    return certification_summary(conn)[RESULT_CERTIFIED]


def certification_history(conn: sqlite3.Connection, source_id: int, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM source_certifications
         WHERE source_id = ?
         ORDER BY id DESC
         LIMIT ?
        """,
        (source_id, limit),
    ).fetchall()
