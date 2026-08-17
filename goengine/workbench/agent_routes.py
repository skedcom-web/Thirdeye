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

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .. import acquisition, pipeline, registry
from ..db import utcnow
from ..extraction import ocr as ocrengine
from ..operations import agent_auth
from .deps import Config, Conn, RequireAgentKey


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
