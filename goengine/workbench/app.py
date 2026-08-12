"""Module 7 -- Validation Workbench (FastAPI).

Shows the original PDF beside the extracted text and the structured fields,
each with its confidence and the source text it came from, and records the
reviewer's decision. Deliberately minimal: Phase 1 is measured on
traceability, not on UI polish.

The reviewer identity is taken from a form field. This is a POC with no
authentication -- see the README before exposing it beyond localhost.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Iterator

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import audit, registry, repository, review
from ..config import Settings
from ..db import connect, init_db
from ..discovery import crawler
from ..extraction import metadata as meta
from ..extraction.text import load_pages

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


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
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
