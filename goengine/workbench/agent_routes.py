"""Phase 3.4 -- Local Extraction Agent sync API.

A machine-facing counterpart to operations_routes.py: instead of a browser
session, callers authenticate with a bearer token (see deps.py's
RequireAgentKey / operations/agent_auth.py) and post documents a local agent
already discovered, downloaded, and (if needed) OCR'd on hardware that has
Tesseract -- see the design note in pipeline.py's `parse_document` for why
OCR results, specifically, cross this trust boundary while field extraction
never does.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated

from fastapi import Body, FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .. import acquisition, pipeline, registry, repository
from ..db import utcnow
from ..extraction import ocr as ocrengine
from ..operations import agent_auth, extraction_queue
from .deps import Config, Conn, RequireAgentKey

_PROGRESS_FIELDS = (
    "sources_total", "sources_completed",
    "documents_found", "documents_downloaded", "documents_parsed", "documents_failed",
)


def register(app: FastAPI) -> None:
    @app.post("/api/agent/sync/document")
    def sync_document(
        conn: Conn, settings: Config, agent_key: RequireAgentKey,
        file: UploadFile, payload: Annotated[str, Form()],
    ):
        try:
            meta = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"payload is not valid JSON: {exc}") from exc

        try:
            source_id = int(meta["source_id"])
            source_url = str(meta["source_url"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"payload missing/invalid source_id or source_url: {exc}") from exc
        link_text = str(meta.get("link_text") or "")
        file_name = str(meta.get("file_name") or file.filename or "document.pdf")

        ocr_output = None
        raw_pages = meta.get("ocr_pages") or []
        if raw_pages:
            ocr_output = ocrengine.OcrOutput(
                engine="tesseract",
                engine_version=str(meta.get("ocr_engine_version") or "unknown"),
                languages=str(meta.get("ocr_languages") or ""),
                pages=[
                    ocrengine.OcrPageResult(
                        page_number=int(p["page_number"]),
                        text=str(p.get("text") or ""),
                        mean_confidence=float(p.get("mean_confidence") or 0.0),
                    )
                    for p in raw_pages
                ],
            )

        file_bytes = file.file.read()

        def log_attempt(*, ok: bool, document_id: int | None, go_record_id: int | None,
                         is_new_version: bool, sha256: str | None, error: str | None) -> None:
            conn.execute(
                """
                INSERT INTO agent_sync_log
                    (agent_key_id, source_id, source_url, document_id, go_record_id,
                     sha256, byte_size, is_new_version, ok, error, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_key.id, source_id, source_url, document_id, go_record_id,
                    sha256, len(file_bytes), int(is_new_version), int(ok), error, utcnow(),
                ),
            )

        try:
            if not acquisition.looks_like_pdf(file_bytes, "application/pdf"):
                raise ValueError(f"{file_name}: not a PDF")
            registry.assert_approved(source_url)
            if registry.get_source(conn, source_id) is None:
                raise LookupError(f"no source with id {source_id}")
        except registry.SourceRejected as exc:
            log_attempt(ok=False, document_id=None, go_record_id=None, is_new_version=False, sha256=None, error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LookupError as exc:
            log_attempt(ok=False, document_id=None, go_record_id=None, is_new_version=False, sha256=None, error=str(exc))
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            log_attempt(ok=False, document_id=None, go_record_id=None, is_new_version=False, sha256=None, error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            document_id, go_record_id, is_new_version = pipeline.ingest_document_bytes(
                conn, settings, file_bytes,
                source_id=source_id, source_url=source_url, file_name=file_name, link_text=link_text,
                precomputed_ocr=ocr_output, actor=f"agent:{agent_key.label}",
            )
        except (registry.SourceRejected, LookupError, ValueError) as exc:
            log_attempt(ok=False, document_id=None, go_record_id=None, is_new_version=False, sha256=None, error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # The durable copy: local disk (even Render's persistent disk) is
        # only scratch space the parse pipeline just read from -- see
        # repository.store_blob's docstring. A failure here must fail the
        # whole sync (agent_synced_at stays NULL so a retry picks it back
        # up) rather than leave a documents row with nothing durable behind
        # it, which is exactly the "file missing from repository" bug this
        # table exists to close.
        try:
            repository.store_blob(conn, document_id, file_bytes)
        except Exception as exc:
            log_attempt(
                ok=False, document_id=document_id, go_record_id=go_record_id,
                is_new_version=is_new_version, sha256=None, error=f"durable blob store failed: {exc}",
            )
            raise HTTPException(status_code=500, detail=f"archived but durable blob store failed: {exc}") from exc

        sha256_row = conn.execute("SELECT sha256 FROM documents WHERE id = ?", (document_id,)).fetchone()
        sha256 = sha256_row["sha256"] if sha256_row else None
        already_synced = not is_new_version
        log_attempt(
            ok=True, document_id=document_id, go_record_id=go_record_id,
            is_new_version=is_new_version, sha256=sha256, error=None,
        )

        return JSONResponse(
            {
                "document_id": document_id,
                "go_record_id": go_record_id,
                "is_new_version": is_new_version,
                "already_synced": already_synced,
            }
        )

    @app.get("/api/agent/sources")
    def agent_sources(conn: Conn, agent_key: RequireAgentKey):
        """The current active Source Registry, for a local agent to mirror
        into its own database before executing a claimed request -- the
        cloud portal's registry stays the single source of truth (sources
        are added/edited there), the agent just needs a local copy to run
        pipeline.run_all(source_id=...) against."""
        sources = registry.list_sources(conn, active_only=True)
        return JSONResponse(
            [
                {
                    "name": s.name, "department": s.department, "url": s.url,
                    "source_type": s.source_type, "adapter": s.adapter,
                    "crawl_frequency": s.crawl_frequency, "priority": s.priority,
                    "source_category": s.source_category,
                }
                for s in sources
            ]
        )

    @app.post("/api/agent/queue/claim")
    def agent_queue_claim(conn: Conn, agent_key: RequireAgentKey):
        row = extraction_queue.claim_next(conn, agent_key_id=agent_key.id)
        if row is None:
            return JSONResponse({"request": None})
        return JSONResponse({"request": extraction_queue.claim_payload(conn, row)})

    def _require_claiming_agent(conn: sqlite3.Connection, request_id: int, agent_key: agent_auth.AgentKey):
        row = extraction_queue.get_request(conn, request_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such extraction request")
        if row["claimed_by_agent_key_id"] != agent_key.id:
            raise HTTPException(status_code=403, detail="this request was not claimed by your agent key")
        return row

    @app.post("/api/agent/queue/{request_id}/progress")
    def agent_queue_progress(
        request_id: int, conn: Conn, agent_key: RequireAgentKey, body: Annotated[dict, Body()] = {},
    ):
        _require_claiming_agent(conn, request_id, agent_key)
        fields = {k: body[k] for k in _PROGRESS_FIELDS if k in body}
        try:
            # yield_and_requeue: this request hit cli.py's time budget with
            # real work still left -- sent back to QUEUED (never COMPLETED)
            # so claim_next() can hand the next slice out fairly (this
            # request included, once its turn comes back around) instead of
            # the request misreporting itself as fully done after only a
            # fraction of its scope.
            if body.get("yield_and_requeue"):
                status = extraction_queue.yield_request(conn, request_id, **fields)
            else:
                status = extraction_queue.report_progress(conn, request_id, **fields)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # `status` lets the agent notice an admin's Cancel click within one
        # progress report (now firing once per document, not once per
        # batch) instead of grinding on for however long its current source
        # takes to exhaust or hit its time budget -- see cli.py's handling
        # of a non-RUNNING/CLAIMED status back from this call.
        return JSONResponse({"ok": True, "status": status})

    @app.post("/api/agent/queue/{request_id}/complete")
    def agent_queue_complete(
        request_id: int, conn: Conn, agent_key: RequireAgentKey, body: Annotated[dict, Body()] = {},
    ):
        _require_claiming_agent(conn, request_id, agent_key)
        progress_fields = {k: body[k] for k in _PROGRESS_FIELDS if k in body}
        if progress_fields:
            extraction_queue.report_progress(conn, request_id, **progress_fields)
        extraction_queue.complete_request(
            conn, request_id, ok=bool(body.get("ok", True)), error=body.get("error"),
        )
        return JSONResponse({"ok": True})
