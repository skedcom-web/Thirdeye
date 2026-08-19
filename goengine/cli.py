"""`thirdeye` command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import audit, benchmark, pipeline, registry, repository, review
from .certification import calibration as calib
from .certification import categorize
from .certification import failures as failure_intel
from .certification import golden as golden_real
from .certification import run_full_certification
from .certification.benchmark import TARGETS as CERT_TARGETS
from .certification.sources import (
    certification_summary,
    certify_all,
    certify_source,
)
from .config import Settings
from .db import init_db, session, utcnow
from .discovery import crawler
from .discovery.adapters import available as available_adapters
from .extraction.metadata import ALL_FIELDS
from .fetching import HttpFetcher, OfflineFetcher
from .operations import extraction_queue


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.load(getattr(args, "data_dir", None))


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("  (none)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    print("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  " + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    settings = _settings(args)
    with session(settings) as conn:
        ids = registry.seed(conn) if not args.no_seed else []
    print(f"Initialised database at {settings.db_path}")
    print(f"Document repository    {settings.repository_dir}")
    if ids:
        print(f"Seeded {len(ids)} official sources (thirdeye sources list)")
    return 0


def cmd_sources_list(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        sources = registry.list_sources(conn)
        _print_table(
            ["ID", "NAME", "DEPARTMENT", "TYPE", "ADAPTER", "ACTIVE", "FREQ", "LAST CRAWL"],
            [
                [
                    s.id, s.name, s.department, s.source_type, s.adapter,
                    "yes" if s.active else "no", s.crawl_frequency, s.last_crawl_at or "never",
                ]
                for s in sources
            ],
        )
    return 0


def cmd_sources_add(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        try:
            source_id = registry.add_source(
                conn,
                name=args.name,
                department=args.department,
                url=args.url,
                source_type=args.type,
                adapter=args.adapter,
                crawl_frequency=args.frequency,
            )
        except registry.SourceRejected as exc:
            print(f"Rejected: {exc}", file=sys.stderr)
            return 2
    print(f"Registered source #{source_id}: {args.name}")
    return 0


def cmd_sources_disable(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        registry.set_active(conn, args.source_id, False, actor=args.actor)
    print(f"Source #{args.source_id} deactivated")
    return 0


def cmd_crawl(args: argparse.Namespace) -> int:
    settings = _settings(args)
    fetcher = HttpFetcher()
    try:
        with session(settings) as conn:
            results = pipeline.run_discovery(
                conn,
                fetcher,
                only_due=not args.force,
                source_id=args.source_id,
                max_pages=args.max_pages,
            )
    finally:
        fetcher.close()

    if not results:
        print("No sources were due. Use --force to crawl anyway.")
        return 0
    _print_table(
        ["SOURCE", "STATUS", "PAGES", "LINKS", "NEW", "DUPLICATE", "ERROR"],
        [
            [r.source_name, r.status, r.pages_fetched, r.links_seen,
             r.new_documents, r.duplicate_documents, (r.error or "")[:50]]
            for r in results
        ],
    )
    return 0 if all(r.status == "ok" for r in results) else 1


def cmd_download(args: argparse.Namespace) -> int:
    settings = _settings(args)
    fetcher = HttpFetcher()
    try:
        with session(settings) as conn:
            report = pipeline.run_downloads(conn, settings, fetcher, limit=args.limit)
    finally:
        fetcher.close()
    print(f"Downloaded {report.succeeded}/{report.processed} ({report.failed} failed)")
    for error in report.errors[:20]:
        print(f"  ! {error}")
    return 0 if report.failed == 0 else 1


def cmd_parse(args: argparse.Namespace) -> int:
    settings = _settings(args)
    with session(settings) as conn:
        report = pipeline.run_parsing(
            conn, settings, limit=args.limit, preferred_backend=args.backend,
            reparse=args.reparse,
        )
    print(f"Parsed {report.succeeded}/{report.processed} ({report.failed} failed)")
    for error in report.errors[:20]:
        print(f"  ! {error}")
    return 0 if report.failed == 0 else 1


def _department_source_ids(conn, departments: list[str]) -> list[int]:
    """Active source ids whose department falls into one of the given
    buckets (Health/Education/Public Works/Rural Development) -- same
    bucketing logic operations/jobs.py's web Certification Job Center
    already uses for its own department filter."""
    rows = conn.execute("SELECT id, department FROM sources WHERE active = 1").fetchall()
    return [
        int(r["id"]) for r in rows
        if categorize.department_bucket_for(r["department"]) in departments
    ]


def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings(args)
    fetcher = HttpFetcher()
    try:
        with session(settings) as conn:
            if args.department:
                source_ids = _department_source_ids(conn, args.department)
                if not source_ids:
                    print(f"No active sources match department(s): {', '.join(args.department)}", file=sys.stderr)
                    return 2
                reports = [
                    pipeline.run_all(
                        conn, settings, fetcher,
                        only_due=not args.force, source_id=sid,
                        max_pages=args.max_pages, limit=args.limit,
                    )
                    for sid in source_ids
                ]
                new_documents = sum(r.new_documents for r in reports)
                crawl_count = sum(len(r.crawl) for r in reports)
                download_succeeded = sum(r.download.succeeded for r in reports)
                download_processed = sum(r.download.processed for r in reports)
                parse_succeeded = sum(r.parse.succeeded for r in reports)
                parse_processed = sum(r.parse.processed for r in reports)
                errors = [e for r in reports for e in (r.download.errors + r.parse.errors)]
            else:
                report = pipeline.run_all(
                    conn, settings, fetcher,
                    only_due=not args.force, source_id=args.source_id,
                    max_pages=args.max_pages, limit=args.limit,
                )
                new_documents = report.new_documents
                crawl_count = len(report.crawl)
                download_succeeded, download_processed = report.download.succeeded, report.download.processed
                parse_succeeded, parse_processed = report.parse.succeeded, report.parse.processed
                errors = report.download.errors + report.parse.errors
    finally:
        fetcher.close()

    print(f"Discovery : {new_documents} new document(s) across {crawl_count} source(s)")
    print(f"Download  : {download_succeeded}/{download_processed} ok")
    print(f"Parse     : {parse_succeeded}/{parse_processed} ok")
    for error in errors[:20]:
        print(f"  ! {error}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    settings = _settings(args)
    path = Path(args.path)
    if not path.exists():
        print(f"No such file: {path}", file=sys.stderr)
        return 2
    with session(settings) as conn:
        try:
            record_id = pipeline.ingest_local_file(
                conn, settings, path, source_id=args.source_id, source_url=args.source_url
            )
        except (registry.SourceRejected, LookupError, ValueError) as exc:
            print(f"Rejected: {exc}", file=sys.stderr)
            return 2
    print(f"Ingested {path.name} as GO record #{record_id}")
    return 0


def _sync_documents(
    conn, settings: Settings, http_client, server_url: str, auth_headers: dict,
    *, source_ids: list[int] | None = None, retry_failed: bool = False, limit: int = 50,
) -> list[list]:
    """Pushes locally-downloaded, not-yet-synced documents to a Third Eye
    server. Shared by `cmd_sync` (a one-shot CLI invocation) and
    `cmd_agent_daemon` (called automatically after a claimed queue request
    finishes downloading). One bad document never aborts the batch, same
    principle as run/download/parse -- it's recorded on that document's
    `agent_sync_error` and the caller moves on. Returns rows of
    [file_name, status, go_record_id, error] for display."""
    sql = "SELECT id, source_id, source_url, file_name, stored_path FROM documents WHERE agent_synced_at IS NULL"
    params: list = []
    if not retry_failed:
        sql += " AND agent_sync_error IS NULL"
    if source_ids is not None:
        if not source_ids:
            return []
        sql += f" AND source_id IN ({','.join('?' * len(source_ids))})"
        params.extend(source_ids)
    sql += " ORDER BY id LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()

    results: list[list] = []
    for row in rows:
        document_id = int(row["id"])
        extraction = conn.execute(
            "SELECT id FROM extractions WHERE document_id = ? ORDER BY id DESC LIMIT 1",
            (document_id,),
        ).fetchone()
        ocr_pages_payload: list[dict] = []
        if extraction is not None:
            ocr_rows = conn.execute(
                """
                SELECT op.page_number, op.text, op.mean_word_confidence
                  FROM ocr_pages op
                  JOIN ocr_runs orun ON orun.id = op.ocr_run_id
                 WHERE orun.extraction_id = ? AND orun.source = 'server'
                 ORDER BY orun.id DESC, op.page_number
                """,
                (extraction["id"],),
            ).fetchall()
            seen_pages: set[int] = set()
            for r in ocr_rows:
                if r["page_number"] in seen_pages:
                    continue
                seen_pages.add(r["page_number"])
                ocr_pages_payload.append(
                    {
                        "page_number": r["page_number"],
                        "text": r["text"],
                        "mean_confidence": r["mean_word_confidence"] or 0.0,
                    }
                )

        payload = {
            "source_id": int(row["source_id"]),
            "source_url": row["source_url"],
            "file_name": row["file_name"],
            "ocr_pages": ocr_pages_payload,
        }
        file_path = repository.absolute_path(settings, row["stored_path"])
        try:
            with open(file_path, "rb") as fh:
                resp = http_client.post(
                    f"{server_url}/api/agent/sync/document",
                    headers=auth_headers,
                    files={"file": (row["file_name"], fh, "application/pdf")},
                    data={"payload": json.dumps(payload)},
                )
            if resp.status_code == 200:
                conn.execute(
                    "UPDATE documents SET agent_synced_at = ?, agent_sync_error = NULL WHERE id = ?",
                    (utcnow(), document_id),
                )
                body = resp.json()
                status = "already-synced" if body.get("already_synced") else "synced"
                results.append([row["file_name"], status, body.get("go_record_id"), ""])
            else:
                error = f"HTTP {resp.status_code}: {resp.text[:150]}"
                conn.execute("UPDATE documents SET agent_sync_error = ? WHERE id = ?", (error, document_id))
                results.append([row["file_name"], "failed", "", error])
        except Exception as exc:  # httpx.HTTPError or a filesystem error -- never abort the batch
            error = str(exc)
            conn.execute("UPDATE documents SET agent_sync_error = ? WHERE id = ?", (error, document_id))
            results.append([row["file_name"], "failed", "", error])

    return results


def cmd_sync(args: argparse.Namespace) -> int:
    """Phase 3.4 -- push locally-downloaded (and locally-OCR'd) documents to
    a Third Eye server. Pairs with `thirdeye run --data-dir <local>`, which
    does the discovery/download/parse/OCR that this command then syncs:

        thirdeye run --data-dir localdata && thirdeye sync --server-url https://...
    """
    settings = _settings(args)
    server_url = args.server_url.rstrip("/")
    api_key = args.api_key or os.environ.get("THIRDEYE_AGENT_KEY")
    if not api_key:
        print("No API key: pass --api-key or set THIRDEYE_AGENT_KEY", file=sys.stderr)
        return 2

    import httpx

    source_ids = None
    with session(settings) as conn:
        if args.source_id is not None:
            source_ids = [args.source_id]
        elif args.department:
            source_ids = _department_source_ids(conn, args.department)
            if not source_ids:
                print(f"No active sources match department(s): {', '.join(args.department)}", file=sys.stderr)
                return 2

        if args.resync_all:
            reset_sql = "UPDATE documents SET agent_synced_at = NULL, agent_sync_error = NULL"
            params: list = []
            if source_ids is not None:
                reset_sql += f" WHERE source_id IN ({','.join('?' * len(source_ids))})"
                params.extend(source_ids)
            n = conn.execute(reset_sql, params).rowcount
            print(f"--resync-all: marked {n} already-synced document(s) for re-push")

        auth_headers = {"Authorization": f"Bearer {api_key}"}
        with httpx.Client(timeout=60.0) as client:
            results = _sync_documents(
                conn, settings, client, server_url, auth_headers,
                source_ids=source_ids, retry_failed=args.retry_failed, limit=args.limit,
            )

    failed = sum(1 for r in results if r[1] == "failed")
    _print_table(["DOCUMENT", "STATUS", "SERVER RECORD ID", "ERROR"], results)
    print(f"\n{len(results) - failed}/{len(results)} synced")
    return 1 if failed else 0


def _mirror_sources_from_server(conn, http_client, server_url: str, auth_headers: dict) -> None:
    """Pulls the server's active Source Registry and upserts by name into
    the local database. The server stays the single source of truth for
    where sources come from (added/edited in the web UI) -- this just gives
    a local run something to point pipeline.run_all(source_id=...) at."""
    resp = http_client.get(f"{server_url}/api/agent/sources", headers=auth_headers)
    resp.raise_for_status()
    for spec in resp.json():
        existing = registry.get_by_name(conn, spec["name"])
        if existing is not None:
            continue
        try:
            registry.add_source(
                conn, name=spec["name"], department=spec["department"], url=spec["url"],
                source_type=spec["source_type"], adapter=spec.get("adapter") or "generic_links",
                crawl_frequency=spec.get("crawl_frequency") or "daily",
                priority=spec.get("priority") or "Medium", source_category=spec.get("source_category"),
                actor="agent-daemon",
            )
        except registry.SourceRejected as exc:
            print(f"  ! could not mirror source {spec['name']!r}: {exc}")


_BATCH_SIZE = 10  # small on purpose -- see _run_claimed_request docstring


def _run_claimed_request(
    conn, settings: Settings, fetcher, http_client, server_url: str, auth_headers: dict, req: dict,
) -> None:
    """A source can legitimately have hundreds of real documents (a real TN
    department listing has produced 500+ in practice) -- downloading and
    parsing all of them for real, at a polite per-host crawl delay, can take
    a long time. Processing a source in one giant pipeline.run_all() call
    means the admin sees nothing land in the portal and no progress update
    for the entire duration. Instead this repeatedly calls pipeline.run_all
    with a small `limit`, syncing and reporting progress after every small
    batch, until a batch does no new work -- so results appear within
    seconds/minutes rather than only once an entire large source finishes,
    and a killed/interrupted run keeps whatever batches already landed."""
    request_id = req["id"]
    source_ids = extraction_queue.resolve_local_source_ids(
        conn, state_name=req.get("state_name"), district_name=req.get("district_name"),
        department_filter=req.get("department_filter"),
    )
    if not source_ids:
        raise RuntimeError("no local sources match this request's scope (state/district/department)")

    sources_completed = documents_found = documents_downloaded = documents_parsed = documents_failed = 0
    synced_total = sync_failed_total = 0
    http_client.post(
        f"{server_url}/api/agent/queue/{request_id}/progress", headers=auth_headers,
        json={"sources_total": len(source_ids), "sources_completed": 0},
    )
    for sid in source_ids:
        while True:
            report = pipeline.run_all(
                conn, settings, fetcher, only_due=False, source_id=sid, max_pages=5, limit=_BATCH_SIZE,
            )
            documents_found += report.new_documents
            documents_downloaded += report.download.succeeded
            documents_parsed += report.parse.succeeded
            documents_failed += report.download.failed + report.parse.failed

            sync_results = _sync_documents(
                conn, settings, http_client, server_url, auth_headers, source_ids=[sid], limit=1000,
            )
            batch_synced = len(sync_results)
            batch_sync_failed = sum(1 for r in sync_results if r[1] == "failed")
            synced_total += batch_synced
            sync_failed_total += batch_sync_failed

            print(
                f"  source {sid}, batch: {report.download.succeeded} downloaded, "
                f"{report.parse.succeeded} parsed, {batch_synced - batch_sync_failed}/{batch_synced} synced "
                f"(running total: {documents_downloaded} downloaded, {documents_parsed} parsed)"
            )
            http_client.post(
                f"{server_url}/api/agent/queue/{request_id}/progress", headers=auth_headers,
                json={
                    "sources_completed": sources_completed, "documents_found": documents_found,
                    "documents_downloaded": documents_downloaded, "documents_parsed": documents_parsed,
                    "documents_failed": documents_failed,
                },
            )

            # Nothing new discovered, downloaded, or parsed this batch --
            # this source is exhausted, move to the next one.
            if report.new_documents == 0 and report.download.succeeded == 0 and report.parse.succeeded == 0:
                break

        sources_completed += 1
        http_client.post(
            f"{server_url}/api/agent/queue/{request_id}/progress", headers=auth_headers,
            json={
                "sources_completed": sources_completed, "documents_found": documents_found,
                "documents_downloaded": documents_downloaded, "documents_parsed": documents_parsed,
                "documents_failed": documents_failed,
            },
        )

    print(
        f"  done: sources {sources_completed}/{len(source_ids)}, "
        f"{documents_downloaded} downloaded, {documents_parsed} parsed, "
        f"{synced_total - sync_failed_total}/{synced_total} synced"
    )

    http_client.post(
        f"{server_url}/api/agent/queue/{request_id}/complete", headers=auth_headers,
        json={"ok": True},
    )


def cmd_agent_daemon(args: argparse.Namespace) -> int:
    """Phase 3.4 -- polls a Third Eye server for queued Local extraction
    requests (created from the Extraction Center's "Local Agent" mode),
    executes them against this machine's own unblocked network, and syncs
    results back automatically. Leave this running on a machine that can
    reach the government sites; Ctrl+C stops it after the current cycle."""
    settings = _settings(args)
    server_url = args.server_url.rstrip("/")
    api_key = args.api_key or os.environ.get("THIRDEYE_AGENT_KEY")
    if not api_key:
        print("No API key: pass --api-key or set THIRDEYE_AGENT_KEY", file=sys.stderr)
        return 2

    import signal
    import time

    import httpx

    stop = {"flag": False}

    def _handle_sigint(signum, frame):
        stop["flag"] = True
        print("\nStopping after the current cycle...")

    signal.signal(signal.SIGINT, _handle_sigint)

    auth_headers = {"Authorization": f"Bearer {api_key}"}
    fetcher = HttpFetcher()
    if args.once:
        print(f"Agent daemon: checking {server_url} once (--once).")
    else:
        print(f"Agent daemon started. Polling {server_url} every {args.poll_interval}s. Ctrl+C to stop.")
    try:
        with httpx.Client(timeout=120.0) as client:
            while not stop["flag"]:
                try:
                    resp = client.post(f"{server_url}/api/agent/queue/claim", headers=auth_headers)
                    resp.raise_for_status()
                    req = resp.json().get("request")
                except httpx.HTTPError as exc:
                    print(f"! poll failed: {exc}")
                    req = None

                if req is None:
                    if args.once:
                        break
                    time.sleep(args.poll_interval)
                    continue

                print(f"Claimed request #{req['id']}: department_filter={req.get('department_filter')}")
                with session(settings) as conn:
                    try:
                        _mirror_sources_from_server(conn, client, server_url, auth_headers)
                        _run_claimed_request(conn, settings, fetcher, client, server_url, auth_headers, req)
                    except Exception as exc:
                        print(f"! request #{req['id']} failed: {exc}")
                        try:
                            client.post(
                                f"{server_url}/api/agent/queue/{req['id']}/complete",
                                headers=auth_headers, json={"ok": False, "error": str(exc)},
                            )
                        except httpx.HTTPError:
                            pass
                if args.once:
                    break
    finally:
        fetcher.close()

    print("Agent daemon stopped.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings(args)
    with session(settings) as conn:
        discovery = crawler.counts_by_status(conn)
        reviews = review.counts_by_status(conn)
        stats = repository.stats(settings, conn)

    print("Discovery")
    _print_table(["STATUS", "COUNT"], [[k, v] for k, v in discovery.items()])
    print("\nVerification")
    _print_table(["STATUS", "COUNT"], [[k, v] for k, v in reviews.items()])
    print("\nRepository")
    _print_table(
        ["METRIC", "VALUE"],
        [
            ["documents", stats["documents"]],
            ["unique files", stats["unique_files"]],
            ["total MB", f"{stats['total_bytes'] / 1048576:.1f}"],
        ],
    )
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        rows = review.queue(conn, status=args.status, limit=args.limit)
        _print_table(
            ["RECORD", "FILE", "SOURCE", "FIELDS", "MIN CONF", "OCR"],
            [
                [
                    r["id"], r["file_name"], r["source_name"], r["field_count"],
                    f"{(r['min_field_confidence'] or 0):.2f}",
                    "yes" if r["needs_ocr"] else "no",
                ]
                for r in rows
            ],
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        try:
            summary = review.get_summary(conn, args.record_id)
        except LookupError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        print(f"Record #{summary.record_id}  [{summary.status}]")
        print(f"  file        {summary.file_name}")
        print(f"  source      {summary.source_name}")
        print(f"  source url  {summary.source_url}")
        print(f"  sha256      {summary.sha256}")
        print(f"  text layer  {summary.page_count} pages, "
              f"confidence {summary.extraction_confidence:.1%}"
              f"{'  [NEEDS OCR]' if summary.needs_ocr else ''}")
        print("\n  Fields")
        for name in ALL_FIELDS:
            data = summary.fields.get(name)
            if data is None:
                print(f"    {name:<12} (not found)")
                continue
            print(f"    {name:<12} {data['normalized_value']}")
            print(f"    {'':<12} page {data['source_page']} | "
                  f"confidence {data['confidence']:.1%} | {data['method']} | {data['origin']}")
            print(f"    {'':<12} evidence: \"{data['source_text'][:100]}\"")
        if summary.missing_core_fields:
            print(f"\n  Missing core fields: {', '.join(summary.missing_core_fields)}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        try:
            review.approve(
                conn, args.record_id, reviewer=args.reviewer, note=args.note,
                allow_missing_fields=args.allow_missing,
            )
        except (review.ReviewError, LookupError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(f"Record #{args.record_id} approved by {args.reviewer}")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        try:
            review.reject(conn, args.record_id, reviewer=args.reviewer, reason=args.reason)
        except (review.ReviewError, LookupError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(f"Record #{args.record_id} rejected by {args.reviewer}")
    return 0


def cmd_correct(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        try:
            review.correct_field(
                conn, args.record_id, args.field, args.value,
                reviewer=args.reviewer, source_page=args.page, note=args.note,
            )
        except (review.ReviewError, LookupError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(f"Record #{args.record_id}: {args.field} corrected by {args.reviewer}")
    return 0


def cmd_verified(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        records = review.verified_records(conn, limit=args.limit)
    print(json.dumps(records, indent=2, ensure_ascii=False))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        if args.document_id is not None:
            entries = audit.document_provenance(conn, args.document_id)
        else:
            entries = audit.trail(
                conn, entity_type=args.entity_type, entity_id=args.entity_id, limit=args.limit
            )
        _print_table(
            ["TS", "ACTOR", "ACTION", "ENTITY", "FIELD", "BEFORE", "AFTER"],
            [
                [
                    e.ts, e.actor, e.action, f"{e.entity_type}#{e.entity_id}",
                    e.field_name or "", (e.before_value or "")[:28], (e.after_value or "")[:28],
                ]
                for e in entries
            ],
        )
    return 0


def cmd_verify_repo(args: argparse.Namespace) -> int:
    settings = _settings(args)
    with session(settings) as conn:
        results = repository.verify_all(settings, conn)
    failures = [r for r in results if not r[1]]
    print(f"Verified {len(results)} document(s); {len(failures)} problem(s)")
    for document_id, _, message in failures:
        print(f"  ! document #{document_id}: {message}")
    return 0 if not failures else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .workbench.app import create_app

    settings = _settings(args)
    print(f"Validation Workbench -> http://{args.host}:{args.port}")
    print(f"Data directory        {settings.data_dir}")
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_golden_init(args: argparse.Namespace) -> int:
    dataset_dir = Path(args.dataset)
    dataset_dir.mkdir(parents=True, exist_ok=True)

    if args.with_samples:
        from .sampledata import write_samples

        written = write_samples(dataset_dir)
        rows = [sample.annotation(path.name) for sample, path in written]
        path = benchmark.write_annotation_template(dataset_dir, rows)
        print(f"Wrote {len(written)} sample GO PDFs and pre-filled {path}")
        print("These are SYNTHETIC fixtures for smoke-testing the harness, not real GOs.")
    else:
        path = benchmark.write_annotation_template(dataset_dir)
        pdfs = len(list(dataset_dir.glob("*.pdf")))
        print(f"Wrote {path} covering {pdfs} PDF(s) in {dataset_dir}")
        print("Fill in the ground-truth columns, then run: thirdeye golden score")
    return 0


def cmd_golden_score(args: argparse.Namespace) -> int:
    dataset_dir = Path(args.dataset)
    try:
        report = benchmark.score_dataset(dataset_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(benchmark.format_report(report, show_mistakes=not args.quiet))
    if args.json:
        path = benchmark.write_json_report(report, Path(args.json))
        print(f"\nJSON report written to {path}")
    return 0 if report.phase1_pass else 1


def cmd_demo(args: argparse.Namespace) -> int:
    """Run the whole pipeline offline against synthetic GOs."""
    from .sampledata import write_samples

    settings = Settings.load(args.data_dir or "data/demo")
    settings.ensure_dirs()
    staging = settings.data_dir / "staging"
    written = write_samples(staging)

    base_url = "https://cms.tn.gov.in/sites/default/files/go"
    listing_url = "https://cms.tn.gov.in/go-search"

    rows = "\n".join(
        f'<tr><td>{sample.go_number}</td><td>{sample.printed_date}</td>'
        f'<td>{sample.department}</td>'
        f'<td><a href="{base_url}/{path.name}">{sample.go_number} - abstract</a></td></tr>'
        for sample, path in written
    )
    listing_html = (
        "<html><body><h1>Government Orders</h1><table>"
        "<tr><th>G.O. No</th><th>Date</th><th>Department</th><th>Subject</th></tr>"
        f"{rows}</table></body></html>"
    )

    fetcher = OfflineFetcher()
    fetcher.add_html(listing_url, listing_html)
    for _, path in written:
        fetcher.add_bytes(f"{base_url}/{path.name}", path.read_bytes())

    with session(settings) as conn:
        source = registry.get_by_name(conn, "Tamil Nadu GO Portal")
        source_id = source.id if source else registry.seed(conn)[0]
        report = pipeline.run_all(
            conn, settings, fetcher, only_due=False, source_id=source_id
        )
        print(f"Discovery : {report.new_documents} new document(s)")
        print(f"Download  : {report.download.succeeded}/{report.download.processed} ok")
        print(f"Parse     : {report.parse.succeeded}/{report.parse.processed} ok")
        for error in (report.download.errors + report.parse.errors):
            print(f"  ! {error}")

        print("\nVerification queue:")
        rows_out = review.queue(conn, limit=20)
        _print_table(
            ["RECORD", "FILE", "GO NUMBER", "DATE", "DEPARTMENT", "MIN CONF"],
            [
                [
                    r["id"], r["file_name"],
                    (review.get_summary(conn, int(r["id"])).fields.get("go_number") or {}).get(
                        "normalized_value", "-"
                    ),
                    (review.get_summary(conn, int(r["id"])).fields.get("go_date") or {}).get(
                        "normalized_value", "-"
                    ),
                    (review.get_summary(conn, int(r["id"])).fields.get("department") or {}).get(
                        "normalized_value", "-"
                    ),
                    f"{(r['min_field_confidence'] or 0):.2f}",
                ]
                for r in rows_out
            ],
        )

    print(f"\nDemo data in {settings.data_dir}")
    print(f"Inspect it with:  thirdeye serve --data-dir {settings.data_dir}")
    return 0


# ---------------------------------------------------------------------------
# Phase 2 -- Module 1: Source Certification
# ---------------------------------------------------------------------------
def cmd_certify_sources(args: argparse.Namespace) -> int:
    settings = _settings(args)
    fetcher = HttpFetcher()
    try:
        with session(settings) as conn:
            if args.source_id is not None:
                results = [
                    certify_source(
                        conn, settings, fetcher, args.source_id, max_pages=args.max_pages,
                        download_sample=args.download_sample,
                    )
                ]
            else:
                results = certify_all(
                    conn, settings, fetcher, max_pages=args.max_pages,
                    download_sample=args.download_sample,
                )
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        fetcher.close()

    _print_table(
        ["SOURCE", "RESULT", "CONNECT", "DISCOVER", "DOWNLOAD", "STABLE", "AUTHENTIC", "DOCS"],
        [
            [
                r.source_id, r.result,
                "ok" if r.connectivity_ok else "fail", "ok" if r.discovery_ok else "fail",
                "ok" if r.download_ok else "fail", "ok" if r.stability_ok else "fail",
                "ok" if r.authenticity_ok else "fail", r.documents_discovered,
            ]
            for r in results
        ],
    )
    return 0 if all(r.result == "CERTIFIED" for r in results) else 1


def cmd_certify_status(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        summary = certification_summary(conn)
        print(f"Certified: {summary['CERTIFIED']}  Partial: {summary['PARTIALLY_CERTIFIED']}  "
              f"Failed: {summary['FAILED']}  Pending: {summary['PENDING']}")
        print(f"Target: 10+ certified sources ({'MET' if summary['CERTIFIED'] >= 10 else 'not yet met'})\n")

        rows = conn.execute(
            "SELECT id, name, certification_status, certification_date, "
            "last_crawl_success_at, last_crawl_failure_at FROM sources ORDER BY id"
        ).fetchall()
        _print_table(
            ["ID", "NAME", "STATUS", "CERTIFIED AT", "LAST SUCCESS", "LAST FAILURE"],
            [
                [r["id"], r["name"], r["certification_status"], r["certification_date"] or "-",
                 r["last_crawl_success_at"] or "-", r["last_crawl_failure_at"] or "-"]
                for r in rows
            ],
        )
    return 0


# ---------------------------------------------------------------------------
# Phase 2 -- Module 2: Acquisition progress
# ---------------------------------------------------------------------------
def cmd_certify_progress(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        progress = categorize.acquisition_progress(conn)
    _print_table(
        ["DEPARTMENT", "COLLECTED", "TARGET", "MET"],
        [
            [bucket, info["count"], info["target"], "yes" if info["met"] else "no"]
            for bucket, info in progress["departments"].items()
        ],
    )
    print(f"\nTotal: {progress['total_in_target_departments']}/{progress['total_target']} "
          f"({'MET' if progress['target_met'] else 'not yet met'})")
    print(f"Other departments: {progress['other_department_documents']}")
    print(f"By text type: {progress['by_text_type']}")
    print(f"By language: {progress['by_language']}")
    print(f"Annexure-heavy: {progress['annexure_heavy_count']}  Table-heavy: {progress['table_heavy_count']}")
    return 0


# ---------------------------------------------------------------------------
# Phase 2 -- Module 3: Golden Dataset (real documents)
# ---------------------------------------------------------------------------
def cmd_golden2_add(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        try:
            golden_id = golden_real.add_to_golden_set(
                conn, args.document_id, added_by=args.added_by, notes=args.notes
            )
        except (golden_real.GoldenSetError, LookupError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(f"Document #{args.document_id} added to golden set as #{golden_id}")
    return 0


def cmd_golden2_annotate(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        try:
            golden_real.annotate_field(
                conn, args.golden_document_id, args.field,
                None if args.absent else args.value,
                annotator=args.annotator, note=args.note,
            )
        except (golden_real.GoldenSetError, LookupError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(f"Golden document #{args.golden_document_id}: {args.field} annotated by {args.annotator}")
    return 0


def cmd_golden2_list(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        rows = golden_real.list_golden_documents(conn)
        _print_table(
            ["ID", "DOCUMENT", "FILE", "DEPARTMENT", "LANGUAGE", "ANNOTATED"],
            [
                [r["id"], r["document_id"], r["file_name"], r["department_bucket"] or "?",
                 r["language"] or "?", f"{r['annotated_fields']}/{len(golden_real.SCORED_FIELDS)}"]
                for r in rows
            ],
        )
    return 0


def cmd_golden2_candidates(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        rows = golden_real.candidates_for_golden_set(conn, limit=args.limit)
        _print_table(
            ["DOCUMENT", "FILE", "SOURCE", "DEPARTMENT", "LANGUAGE"],
            [
                [r["document_id"], r["file_name"], r["source_name"],
                 r["department_bucket"] or "?", r["language"] or "?"]
                for r in rows
            ],
        )
    return 0


# ---------------------------------------------------------------------------
# Phase 2 -- Modules 6, 7, 8: Benchmark, Failures, Calibration
# ---------------------------------------------------------------------------
def cmd_certify_benchmark(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        result = run_full_certification(conn)

    print(f"Run #{result.run_id}: {result.documents_scored} document(s) scored "
          f"({result.skipped_incomplete} skipped, not fully annotated)")
    print(f"Extractor version: {result.extractor_version}\n")

    _print_table(
        ["FIELD", "PRECISION", "RECALL", "F1", "ACCURACY", "TARGET", "SUPPORT", ""],
        [
            [
                name,
                f"{s.precision:.1%}" if s.precision is not None else "-",
                f"{s.recall:.1%}" if s.recall is not None else "-",
                f"{s.f1:.1%}" if s.f1 is not None else "-",
                f"{s.accuracy:.1%}" if s.accuracy is not None else "-",
                f"{CERT_TARGETS.get(name, 0):.0%}",
                s.support,
                "PASS" if (s.accuracy or 0) >= CERT_TARGETS.get(name, 1) and s.support else
                ("FAIL" if s.support else ""),
            ]
            for name, s in result.overall.items()
        ],
    )
    print(f"\nPhase 2 accuracy targets: {'MET' if result.phase2_targets_met else 'NOT MET'}")
    if result.mismatches:
        print(f"{len(result.mismatches)} mismatch(es) recorded as failures "
              "(see: thirdeye certify failures)")
    return 0 if result.phase2_targets_met else 1


def cmd_certify_benchmark_show(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        if args.run_id:
            row = conn.execute(
                "SELECT * FROM certification_benchmark_runs WHERE id = ?", (args.run_id,)
            ).fetchone()
            if row is None:
                print(f"No benchmark run with id {args.run_id}", file=sys.stderr)
                return 2
        else:
            row = conn.execute(
                "SELECT * FROM certification_benchmark_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                print("No certification benchmark has run yet. Run: thirdeye certify benchmark", file=sys.stderr)
                return 1
        print(json.dumps(json.loads(row["summary"]), indent=2))
    return 0


def cmd_certify_failures(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        rows = failure_intel.list_failures(
            conn, failure_type=args.type, field_name=args.field,
            department_bucket=args.department, language=args.language, limit=args.limit,
        )
        _print_table(
            ["ID", "DOCUMENT", "FIELD", "TYPE", "EXPECTED", "ACTUAL", "WHEN"],
            [
                [r["id"], r["document_id"], r["field_name"], r["failure_type"],
                 (r["expected_value"] or "(absent)")[:30], (r["actual_value"] or "(absent)")[:30],
                 r["created_at"]]
                for r in rows
            ],
        )
    return 0


def cmd_certify_calibration(args: argparse.Namespace) -> int:
    with session(_settings(args)) as conn:
        buckets = calib.latest_calibration(conn)
        if not buckets:
            print("No calibration data yet. Run: thirdeye certify benchmark", file=sys.stderr)
            return 1
        error = calib.overall_calibration_error(
            [
                calib.CalibrationBucket(
                    field_name=r["field_name"], bucket_low=r["bucket_low"], bucket_high=r["bucket_high"],
                    predictions_count=r["predictions_count"], correct_count=r["correct_count"],
                    mean_stated_confidence=r["mean_stated_confidence"], actual_accuracy=r["actual_accuracy"],
                )
                for r in buckets
            ]
        )
        _print_table(
            ["FIELD", "BUCKET", "N", "STATED", "ACTUAL", "GAP"],
            [
                [
                    r["field_name"], f"{r['bucket_low']:.1f}-{r['bucket_high']:.1f}",
                    r["predictions_count"], f"{r['mean_stated_confidence']:.1%}",
                    f"{r['actual_accuracy']:.1%}", f"{r['calibration_gap']:+.1%}",
                ]
                for r in buckets
            ],
        )
        print(f"\nWeighted mean calibration error: {error:.1%}" if error is not None else "")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="thirdeye",
        description="Evidence-First Government Order Intelligence Engine (Phase 1)",
    )
    parser.add_argument(
        "--data-dir", help="data directory (default: ./data or $THIRDEYE_DATA_DIR)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the database and seed official sources")
    p.add_argument("--no-seed", action="store_true", help="skip the seed source list")
    p.set_defaults(func=cmd_init)

    # sources
    sources = sub.add_parser("sources", help="manage the official source registry")
    sources_sub = sources.add_subparsers(dest="sources_command", required=True)

    p = sources_sub.add_parser("list", help="list registered sources")
    p.set_defaults(func=cmd_sources_list)

    p = sources_sub.add_parser("add", help="register an official source")
    p.add_argument("--name", required=True)
    p.add_argument("--department", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--type", required=True,
                   choices=["go_portal", "gazette", "department_site"])
    p.add_argument("--adapter", default="generic_links", choices=available_adapters())
    p.add_argument("--frequency", default="daily",
                   choices=["hourly", "daily", "weekly", "manual"])
    p.set_defaults(func=cmd_sources_add)

    p = sources_sub.add_parser("disable", help="deactivate a source")
    p.add_argument("source_id", type=int)
    p.add_argument("--actor", default="operator")
    p.set_defaults(func=cmd_sources_disable)

    # pipeline
    p = sub.add_parser("crawl", help="discover documents on approved sources")
    p.add_argument("--source-id", type=int)
    p.add_argument("--force", action="store_true", help="crawl even if not due")
    p.add_argument("--max-pages", type=int, default=5)
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("download", help="download discovered documents")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("parse", help="extract text and metadata from archived documents")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--backend", choices=["pymupdf", "pdfplumber", "pypdf"])
    p.add_argument("--reparse", action="store_true", help="re-run over already-parsed documents")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("run", help="crawl, download and parse in one pass")
    p.add_argument("--source-id", type=int)
    p.add_argument("--department", action="append", choices=list(categorize.ALL_BUCKETS),
                   help="restrict to sources in this department bucket; repeat for more than one")
    p.add_argument("--force", action="store_true")
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("ingest", help="archive and parse a local PDF")
    p.add_argument("path")
    p.add_argument("--source-id", type=int, required=True)
    p.add_argument("--source-url", required=True, help="official URL this file came from")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("sync", help="Phase 3.4: push locally-downloaded documents to a Third Eye server")
    p.add_argument("--server-url", required=True, help="e.g. https://thirdeye-xyz.onrender.com")
    p.add_argument("--api-key", help="or set THIRDEYE_AGENT_KEY (preferred -- avoids shell history/saved task args)")
    p.add_argument("--source-id", type=int, help="sync only this source's documents; default: all")
    p.add_argument("--department", action="append", choices=list(categorize.ALL_BUCKETS),
                   help="restrict to sources in this department bucket; repeat for more than one")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--retry-failed", action="store_true", help="also re-attempt documents whose last sync failed")
    p.add_argument(
        "--resync-all", action="store_true",
        help="also re-push documents already marked synced -- for recovering after the server's "
             "durable storage lost bytes it never actually had (e.g. before document_blobs "
             "existed). Re-uploads what's already archived on this machine; does not re-download "
             "anything from the source site.",
    )
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser(
        "agent-daemon",
        help="Phase 3.4: poll a Third Eye server for queued Local extraction requests and execute them here",
    )
    p.add_argument("--server-url", required=True, help="e.g. https://thirdeye-xyz.onrender.com")
    p.add_argument("--api-key", help="or set THIRDEYE_AGENT_KEY (preferred -- avoids shell history/saved task args)")
    p.add_argument("--poll-interval", type=int, default=30, help="seconds between checks when the queue is empty")
    p.add_argument("--once", action="store_true",
                   help="process at most one queued request then exit -- for OS-level scheduling "
                        "(Task Scheduler/cron), the blueprint's 'Local Scheduled' mode, instead of "
                        "leaving this running continuously")
    p.set_defaults(func=cmd_agent_daemon)

    # review
    p = sub.add_parser("status", help="pipeline counts")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("queue", help="list records awaiting verification")
    p.add_argument("--status", default="pending", choices=["pending", "approved", "rejected"])
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("show", help="show one record with its evidence")
    p.add_argument("record_id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("approve", help="approve a record")
    p.add_argument("record_id", type=int)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--note")
    p.add_argument("--allow-missing", action="store_true",
                   help="approve despite missing core fields (audited)")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", help="reject a record")
    p.add_argument("record_id", type=int)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("correct", help="correct a field value")
    p.add_argument("record_id", type=int)
    p.add_argument("field", choices=list(ALL_FIELDS))
    p.add_argument("value")
    p.add_argument("--reviewer", required=True)
    p.add_argument("--page", type=int, help="source page for the corrected value")
    p.add_argument("--note")
    p.set_defaults(func=cmd_correct)

    p = sub.add_parser("verified", help="dump the verified GO database as JSON")
    p.add_argument("--limit", type=int, default=500)
    p.set_defaults(func=cmd_verified)

    # governance
    p = sub.add_parser("audit", help="read the audit trail")
    p.add_argument("--entity-type")
    p.add_argument("--entity-id", type=int)
    p.add_argument("--document-id", type=int, help="full provenance chain for one document")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("verify-repo", help="re-hash every archived file")
    p.set_defaults(func=cmd_verify_repo)

    # workbench
    p = sub.add_parser("serve", help="run the Validation Workbench")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    # golden dataset
    golden = sub.add_parser("golden", help="golden dataset and accuracy measurement")
    golden_sub = golden.add_subparsers(dest="golden_command", required=True)

    p = golden_sub.add_parser("init", help="create/refresh the annotation template")
    p.add_argument("--dataset", default="golden", help="dataset directory")
    p.add_argument("--with-samples", action="store_true",
                   help="also write synthetic sample GOs for a harness smoke test")
    p.set_defaults(func=cmd_golden_init)

    p = golden_sub.add_parser("score", help="measure accuracy against the annotations")
    p.add_argument("--dataset", default="golden")
    p.add_argument("--json", help="also write a JSON report to this path")
    p.add_argument("--quiet", action="store_true", help="omit the per-mistake listing")
    p.set_defaults(func=cmd_golden_score)

    p = sub.add_parser("demo", help="run the full pipeline offline on synthetic GOs")
    p.set_defaults(func=cmd_demo)

    # -------------------------------------------------------------------
    # Phase 2 -- certification
    # -------------------------------------------------------------------
    certify = sub.add_parser("certify", help="Phase 2: source certification and accuracy certification")
    certify_sub = certify.add_subparsers(dest="certify_command", required=True)

    p = certify_sub.add_parser("sources", help="run the 5 certification checks against live sources")
    p.add_argument("--source-id", type=int, help="certify one source; default is all active sources")
    p.add_argument("--max-pages", type=int, default=3)
    p.add_argument("--download-sample", type=int, default=3)
    p.set_defaults(func=cmd_certify_sources)

    p = certify_sub.add_parser("status", help="source certification summary")
    p.set_defaults(func=cmd_certify_status)

    p = certify_sub.add_parser("progress", help="Real GO Acquisition Program progress")
    p.set_defaults(func=cmd_certify_progress)

    golden2 = certify_sub.add_parser("golden", help="golden dataset built from real archived documents")
    golden2_sub = golden2.add_subparsers(dest="certify_golden_command", required=True)

    p = golden2_sub.add_parser("add", help="add an archived document to the golden set")
    p.add_argument("document_id", type=int)
    p.add_argument("--added-by", required=True)
    p.add_argument("--notes")
    p.set_defaults(func=cmd_golden2_add)

    p = golden2_sub.add_parser("annotate", help="record ground truth for one field")
    p.add_argument("golden_document_id", type=int)
    p.add_argument("field", choices=list(golden_real.ALL_GOLDEN_FIELDS))
    p.add_argument("value", nargs="?", default=None)
    p.add_argument("--annotator", required=True)
    p.add_argument("--absent", action="store_true", help="assert the field does not appear in the document")
    p.add_argument("--note")
    p.set_defaults(func=cmd_golden2_annotate)

    p = golden2_sub.add_parser("list", help="list documents in the golden set")
    p.set_defaults(func=cmd_golden2_list)

    p = golden2_sub.add_parser("candidates", help="archived documents not yet in the golden set")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_golden2_candidates)

    p = certify_sub.add_parser("benchmark", help="score the golden set: P/R/F1, failures, calibration")
    p.set_defaults(func=cmd_certify_benchmark)

    p = certify_sub.add_parser("benchmark-show", help="print a certification benchmark run as JSON")
    p.add_argument("--run-id", type=int, help="default: most recent run")
    p.set_defaults(func=cmd_certify_benchmark_show)

    p = certify_sub.add_parser("failures", help="query recorded extraction failures")
    p.add_argument("--type", choices=list(failure_intel.ALL_FAILURE_TYPES))
    p.add_argument("--field", choices=list(golden_real.SCORED_FIELDS))
    p.add_argument("--department")
    p.add_argument("--language")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_certify_failures)

    p = certify_sub.add_parser("calibration", help="stated confidence vs. actual accuracy, by bucket")
    p.set_defaults(func=cmd_certify_calibration)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
