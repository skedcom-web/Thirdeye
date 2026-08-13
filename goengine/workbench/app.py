"""Module 7 -- Validation Workbench (FastAPI).

Shows the original PDF beside the extracted text and the structured fields,
each with its confidence and the source text it came from, and records the
reviewer's decision. Deliberately minimal: Phase 1 is measured on
traceability, not on UI polish.

The reviewer identity is taken from a form field. This is a POC with no
authentication -- see the README before exposing it beyond localhost.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated, Iterator

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import audit, registry, repository, review
from ..certification import calibration as calib
from ..certification import categorize
from ..certification import failures as failure_intel
from ..certification import golden, run_full_certification
from ..certification.sources import certification_summary, certify_source
from ..config import Settings
from ..db import connect, init_db
from ..discovery import crawler
from ..extraction import metadata as meta
from ..extraction.text import load_pages
from ..fetching import FetchError, HttpFetcher

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


# Dependencies must live at module scope: with `from __future__ import
# annotations` every annotation is a string, and FastAPI resolves those
# against module globals. A closure-local alias silently degrades into a
# query parameter instead of a dependency.
def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_fetcher() -> Iterator[HttpFetcher]:
    """A real fetcher by default -- overridden in tests via
    app.dependency_overrides so certification tests never touch the network."""
    fetcher = HttpFetcher()
    try:
        yield fetcher
    finally:
        fetcher.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
FetcherDep = Annotated[HttpFetcher, Depends(get_fetcher)]
Config = Annotated[Settings, Depends(get_settings)]

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["pct"] = lambda value: f"{float(value) * 100:.1f}%"


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.load()
    resolved.ensure_dirs()
    # Create the schema once at startup so a fresh checkout can serve
    # immediately; per-request connections stay short-lived.
    init_db(resolved).close()

    app = FastAPI(title="Thirdeye Validation Workbench", version="0.1.0")
    app.state.settings = resolved

    # -----------------------------------------------------------------------
    # Dashboard
    # -----------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, conn: Conn, config: Config):
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "sources": registry.list_sources(conn),
                "discovery_counts": crawler.counts_by_status(conn),
                "review_counts": review.counts_by_status(conn),
                "repo_stats": repository.stats(config, conn),
                "pending": review.queue(conn, limit=50),
                "recent_audit": audit.trail(conn, limit=25),
            },
        )

    # -----------------------------------------------------------------------
    # Review one record
    # -----------------------------------------------------------------------
    @app.get("/records/{record_id}", response_class=HTMLResponse)
    def record_detail(request: Request, record_id: int, conn: Conn):
        try:
            summary = review.get_summary(conn, record_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        candidates: dict[str, list[sqlite3.Row]] = {}
        for row in conn.execute(
            """
            SELECT * FROM go_field_candidates
             WHERE record_id = ?
             ORDER BY field_name, confidence DESC
            """,
            (record_id,),
        ).fetchall():
            candidates.setdefault(row["field_name"], []).append(row)

        return templates.TemplateResponse(
            request,
            "record.html",
            {
                "record": summary,
                "pages": load_pages(conn, summary.extraction_id),
                "core_fields": meta.CORE_FIELDS,
                "optional_fields": meta.OPTIONAL_FIELDS,
                "candidates": candidates,
                "trail": audit.trail(conn, entity_type="go_record", entity_id=record_id),
                "provenance": audit.document_provenance(conn, summary.document_id),
            },
        )

    # -----------------------------------------------------------------------
    # The original PDF, served from the repository
    # -----------------------------------------------------------------------
    @app.get("/documents/{document_id}/pdf")
    def document_pdf(document_id: int, conn: Conn, config: Config):
        row = conn.execute(
            "SELECT stored_path, file_name FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="document not found")
        path = repository.absolute_path(config, row["stored_path"])
        if not path.exists():
            raise HTTPException(status_code=410, detail="file missing from repository")
        # "inline" so the reviewer sees the original beside the extracted
        # fields; FileResponse's `filename=` would force a download instead.
        safe_name = row["file_name"].replace('"', "")
        return FileResponse(
            path,
            media_type="application/pdf",
            headers={"content-disposition": f'inline; filename="{safe_name}"'},
        )

    @app.get("/documents/{document_id}/verify")
    def document_verify(document_id: int, conn: Conn, config: Config):
        ok, message = repository.verify_document(config, conn, document_id)
        return JSONResponse({"document_id": document_id, "ok": ok, "message": message})

    # -----------------------------------------------------------------------
    # Decisions
    # -----------------------------------------------------------------------
    @app.post("/records/{record_id}/correct")
    def post_correct(
        record_id: int,
        conn: Conn,
        reviewer: Annotated[str, Form()],
        field_name: Annotated[str, Form()],
        new_value: Annotated[str, Form()],
        source_page: Annotated[int | None, Form()] = None,
        note: Annotated[str | None, Form()] = None,
    ):
        try:
            review.correct_field(
                conn,
                record_id,
                field_name,
                new_value.strip(),
                reviewer=reviewer.strip(),
                source_page=source_page,
                note=note,
            )
        except (review.ReviewError, LookupError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/records/{record_id}", status_code=303)

    @app.post("/records/{record_id}/approve")
    def post_approve(
        record_id: int,
        conn: Conn,
        reviewer: Annotated[str, Form()],
        note: Annotated[str | None, Form()] = None,
        override: Annotated[str | None, Form()] = None,
    ):
        try:
            review.approve(
                conn,
                record_id,
                reviewer=reviewer.strip(),
                note=note,
                allow_missing_fields=bool(override),
            )
        except (review.ReviewError, LookupError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/", status_code=303)

    @app.post("/records/{record_id}/reject")
    def post_reject(
        record_id: int,
        conn: Conn,
        reviewer: Annotated[str, Form()],
        reason: Annotated[str, Form()],
    ):
        try:
            review.reject(conn, record_id, reviewer=reviewer.strip(), reason=reason.strip())
        except (review.ReviewError, LookupError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/", status_code=303)

    # -----------------------------------------------------------------------
    # Module 3 -- Golden Dataset Workbench
    # -----------------------------------------------------------------------
    @app.get("/golden", response_class=HTMLResponse)
    def golden_list(request: Request, conn: Conn):
        return templates.TemplateResponse(
            request,
            "golden_list.html",
            {
                "progress": categorize.acquisition_progress(conn),
                "summary": golden.golden_set_summary(conn),
                "documents": golden.list_golden_documents(conn),
                "candidates": golden.candidates_for_golden_set(conn, limit=30),
                "scored_fields": golden.SCORED_FIELDS,
            },
        )

    @app.post("/golden/add")
    def golden_add(
        conn: Conn,
        document_id: Annotated[int, Form()],
        added_by: Annotated[str, Form()],
        notes: Annotated[str | None, Form()] = None,
    ):
        try:
            golden_id = golden.add_to_golden_set(
                conn, document_id, added_by=added_by.strip(), notes=notes
            )
        except (golden.GoldenSetError, LookupError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/golden/{golden_id}", status_code=303)

    @app.get("/golden/{golden_document_id}", response_class=HTMLResponse)
    def golden_detail(request: Request, golden_document_id: int, conn: Conn):
        try:
            doc = golden.get_golden_document(conn, golden_document_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        # The machine's own extracted fields are shown as a reference/starting
        # point only -- the annotation itself is a separate, independently
        # stored judgement, never auto-filled from it.
        record_row = conn.execute(
            "SELECT id FROM go_records WHERE document_id = ? ORDER BY id DESC LIMIT 1",
            (doc.document_id,),
        ).fetchone()
        machine_fields = meta.load_fields(conn, int(record_row["id"])) if record_row else {}

        extraction_row = conn.execute(
            "SELECT id FROM extractions WHERE document_id = ? ORDER BY id DESC LIMIT 1",
            (doc.document_id,),
        ).fetchone()
        pages = load_pages(conn, int(extraction_row["id"])) if extraction_row else []

        return templates.TemplateResponse(
            request,
            "golden_annotate.html",
            {
                "doc": doc,
                "machine_fields": machine_fields,
                "pages": pages,
                "scored_fields": golden.SCORED_FIELDS,
                "all_fields": golden.ALL_GOLDEN_FIELDS,
            },
        )

    @app.post("/golden/{golden_document_id}/annotate")
    def golden_annotate(
        golden_document_id: int,
        conn: Conn,
        field_name: Annotated[str, Form()],
        annotator: Annotated[str, Form()],
        value: Annotated[str | None, Form()] = None,
        absent: Annotated[str | None, Form()] = None,
        note: Annotated[str | None, Form()] = None,
    ):
        try:
            golden.annotate_field(
                conn,
                golden_document_id,
                field_name,
                None if absent else value,
                annotator=annotator.strip(),
                note=note,
            )
        except (golden.GoldenSetError, LookupError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/golden/{golden_document_id}", status_code=303)

    # -----------------------------------------------------------------------
    # Module 9 -- Certification Dashboard
    # -----------------------------------------------------------------------
    @app.get("/certification", response_class=HTMLResponse)
    def certification_dashboard(request: Request, conn: Conn, config: Config):
        source_rows = conn.execute(
            """
            SELECT id, name, department, url, certification_status, certification_date,
                   last_crawl_success_at, last_crawl_failure_at
              FROM sources
             ORDER BY id
            """
        ).fetchall()

        latest = conn.execute(
            "SELECT * FROM certification_benchmark_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        latest_summary = json.loads(latest["summary"]) if latest else None

        ocr_count = conn.execute(
            "SELECT COUNT(*) AS n FROM extractions WHERE ocr_applied = 1"
        ).fetchone()["n"]

        calibration_buckets = calib.latest_calibration(conn)
        calibration_error = calib.overall_calibration_error(
            [
                calib.CalibrationBucket(
                    field_name=r["field_name"], bucket_low=r["bucket_low"], bucket_high=r["bucket_high"],
                    predictions_count=r["predictions_count"], correct_count=r["correct_count"],
                    mean_stated_confidence=r["mean_stated_confidence"], actual_accuracy=r["actual_accuracy"],
                )
                for r in calibration_buckets
            ]
        ) if calibration_buckets else None

        return templates.TemplateResponse(
            request,
            "certification.html",
            {
                "source_certification_summary": certification_summary(conn),
                "sources": source_rows,
                "discovery_counts": crawler.counts_by_status(conn),
                "repo_stats": repository.stats(config, conn),
                "ocr_count": ocr_count,
                "golden_summary": golden.golden_set_summary(conn),
                "acquisition_progress": categorize.acquisition_progress(conn),
                "latest_run": latest,
                "latest_summary": latest_summary,
                "run_history": conn.execute(
                    "SELECT id, run_at, documents_scored FROM certification_benchmark_runs ORDER BY id DESC LIMIT 10"
                ).fetchall(),
                "top_failure_types": failure_intel.top_failure_types(conn),
                "failure_trend": failure_intel.failure_trend(conn),
                "department_failures": failure_intel.department_failure_counts(conn),
                "language_failures": failure_intel.language_failure_counts(conn),
                "open_issues": failure_intel.list_failures(conn, limit=25),
                "calibration_buckets": calibration_buckets,
                "calibration_error": calibration_error,
            },
        )

    @app.post("/certification/sources/{source_id}/certify")
    def certify_one_source(source_id: int, conn: Conn, config: Config, fetcher: FetcherDep):
        try:
            certify_source(conn, config, fetcher, source_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FetchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return RedirectResponse("/certification", status_code=303)

    @app.post("/certification/benchmark/run")
    def run_benchmark(conn: Conn):
        run_full_certification(conn)
        return RedirectResponse("/certification", status_code=303)

    # -----------------------------------------------------------------------
    # Verified output + audit
    # -----------------------------------------------------------------------
    @app.get("/api/verified")
    def api_verified(conn: Conn, limit: int = 500):
        return JSONResponse(review.verified_records(conn, limit=limit))

    @app.get("/api/audit")
    def api_audit(
        conn: Conn,
        entity_type: str | None = None,
        entity_id: int | None = None,
        limit: int = 200,
    ):
        entries = audit.trail(conn, entity_type=entity_type, entity_id=entity_id, limit=limit)
        return JSONResponse([entry.__dict__ for entry in entries])

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request, conn: Conn, limit: int = 300):
        return templates.TemplateResponse(
            request, "audit.html", {"entries": audit.trail(conn, limit=limit)}
        )

    return app


app = create_app()
