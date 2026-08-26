"""Resets Third Eye's review/extraction/discovery layer for a production
launch, without touching what the schema treats as permanent evidence.

Several tables are simply undeletable by design (each has its own
BEFORE DELETE trigger that aborts): `documents`/`document_blobs` (the
archived PDF, write-once), `audit_log` (the permanent audit trail),
`golden_documents`/`golden_annotations` (human-verified ground truth), and
`source_versions` (source edit history). This reset works *with* that
design rather than around it: it clears every go_record, extraction, OCR
result, categorization, review decision, and publication status, but the
archived PDFs stay exactly where they are. The next parse run picks them
straight back up -- run_parsing's own guard ("no go_record yet for this
extractor_version") means nothing needs re-crawling or re-downloading;
extraction just starts over from already-safely-archived bytes.

Source registry, geography, staff accounts, and system settings
(Notifications/EmailJS, agent keys) are left untouched throughout --
this is a reset of *content*, not of platform configuration.
"""

from __future__ import annotations

import sqlite3

from .. import audit
from ..db import utcnow
from . import extraction_queue


def reset_for_production(conn: sqlite3.Connection, *, actor: str) -> dict:
    counts: dict[str, int] = {}

    def _delete(table: str, where: str = "") -> None:
        sql = f"DELETE FROM {table}" + (f" WHERE {where}" if where else "")
        counts[table] = conn.execute(sql).rowcount

    # Citizen-facing usage -- pre-launch test accounts and activity, none of
    # it meaningful once every GO they could have interacted with is gone.
    _delete("download_log")
    _delete("saved_records")
    _delete("saved_searches")
    _delete("citizen_sessions")
    _delete("citizen_users")

    # Local-agent operational history and the queued-request log -- testing
    # runs, not something worth preserving once the extraction they cover
    # is being redone.
    _delete("agent_sync_log")
    _delete("extraction_requests")

    # Review layer: escalations and evidence-bound fields, deepest first.
    _delete("escalations")
    _delete("go_field_candidates")
    _delete("go_fields")

    # Accuracy/benchmark history over documents about to lose their records.
    # (golden_documents/golden_annotations are append-only by schema design
    # and are never touched here -- see module docstring.)
    _delete("calibration_snapshots")
    _delete("extraction_failures")
    _delete("certification_benchmark_runs")

    # The records themselves.
    _delete("go_records")
    _delete("document_categories")

    # Extraction/OCR layer -- documents stay archived, but their derived
    # text/analysis is cleared so the next parse starts genuinely fresh.
    _delete("ocr_pages")
    _delete("ocr_runs")
    _delete("extraction_pages")
    _delete("extractions")

    # discovered_documents that never got downloaded have no documents row
    # pointing at them and are safe to remove outright. Ones that DO have an
    # archived document can't be deleted (that document row is permanent and
    # would be left dangling) -- instead their status rolls back to
    # "downloaded", the accurate state now that their parse result is gone.
    _delete("discovered_documents", "status = 'new'")
    conn.execute("UPDATE discovered_documents SET status = 'downloaded' WHERE status IN ('parsed', 'verified', 'rejected')")

    # Crawl history -- operational log of test-phase runs. Surviving
    # discovered_documents rows (ones with an archived document, kept above)
    # can point at a crawl_runs row via first_crawl_run_id; null that out
    # first so it doesn't dangle -- the same reasoning as agent_sync_log.
    conn.execute("UPDATE discovered_documents SET first_crawl_run_id = NULL")
    _delete("crawl_evidences")
    _delete("crawl_runs")
    _delete("certification_jobs")

    # Publication state: nothing is published anymore now that every
    # go_record is gone. Certification status is a property of the source
    # registry (kept as-is), not of content, so it's left alone; only the
    # publication-derived fields roll back.
    districts_reset = conn.execute(
        "UPDATE districts SET status = certification_status WHERE status = 'PUBLISHED'"
    ).rowcount
    conn.execute("UPDATE districts SET publication_status = 'NOT_PUBLISHED' WHERE publication_status != 'NOT_PUBLISHED'")
    departments_reset = conn.execute(
        "UPDATE departments SET publication_status = 'NOT_PUBLISHED', published_by = NULL, published_at = NULL "
        "WHERE publication_status != 'NOT_PUBLISHED'"
    ).rowcount

    # documents.agent_synced_at is local-only bookkeeping on the local
    # agent's own machine (db.py's DOCUMENTS_AGENT_SYNC_COLUMNS docstring) --
    # this reset, running only against the server's database, cannot touch
    # it. Without this, every document the local agent already pushed once
    # looks synced forever even though the go_record/extraction it pointed
    # at was just deleted above, so it silently never gets re-pushed after
    # the next local re-extraction. Queuing a resync_all request here means
    # the local agent's own scheduled daemon (polling every few minutes)
    # clears that bookkeeping and re-pushes everything automatically, with
    # no manual command required after this reset completes.
    resync_request_id = extraction_queue.enqueue_resync_all_request(conn, created_by=actor)

    audit.record(
        conn, action="system.production_reset", entity_type="system", entity_id=None,
        actor=actor,
        detail={
            "table_counts": counts, "districts_reset": districts_reset,
            "departments_reset": departments_reset, "resync_request_id": resync_request_id,
        },
    )

    return {
        "go_records_removed": counts.get("go_records", 0),
        "documents_preserved": True,
        "table_counts": counts,
        "districts_reset": districts_reset,
        "departments_reset": departments_reset,
        "resync_request_id": resync_request_id,
        "reset_at": utcnow(),
    }
