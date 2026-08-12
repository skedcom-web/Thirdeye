"""`thirdeye` command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import audit, benchmark, pipeline, registry, repository, review
from .config import Settings
from .db import init_db, session
from .discovery import crawler
from .discovery.adapters import available as available_adapters
from .extraction.metadata import ALL_FIELDS
from .fetching import HttpFetcher, OfflineFetcher


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


def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings(args)
    fetcher = HttpFetcher()
    try:
        with session(settings) as conn:
            report = pipeline.run_all(
                conn, settings, fetcher,
                only_due=not args.force, source_id=args.source_id,
                max_pages=args.max_pages, limit=args.limit,
            )
    finally:
        fetcher.close()

    print(f"Discovery : {report.new_documents} new document(s) across {len(report.crawl)} source(s)")
    print(f"Download  : {report.download.succeeded}/{report.download.processed} ok")
    print(f"Parse     : {report.parse.succeeded}/{report.parse.processed} ok")
    for error in (report.download.errors + report.parse.errors)[:20]:
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
    p.add_argument("--force", action="store_true")
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("ingest", help="archive and parse a local PDF")
    p.add_argument("path")
    p.add_argument("--source-id", type=int, required=True)
    p.add_argument("--source-url", required=True, help="official URL this file came from")
    p.set_defaults(func=cmd_ingest)

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
