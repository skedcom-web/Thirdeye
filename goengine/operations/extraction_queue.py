"""Phase 3.4 -- Local extraction request queue.

The Extraction Center's "Run Extraction" form has two modes: Cloud (runs
immediately on this server via operations/jobs.py -- unchanged) and Local
(this server can't reach the blocked government sites, so it can only
record the request; a local agent daemon, see cli.py's `agent-daemon`
command, polls for it, executes it on its own unblocked network, and
reports back). This module is the queue itself: enqueue, claim, report
progress, complete.

`state_id`/`district_id` are the server's own geography table PKs and are
NOT meaningful to a separate local agent database -- a local run and the
cloud server each seed their own copy of the same fixed Tamil Nadu
geography independently, so IDs can drift even though names can't. Claim
responses therefore carry `state_name`/`district_name`, and the agent
resolves its own local ids by name, never by cross-database id.
"""

from __future__ import annotations

import json
import sqlite3
import threading

from .. import audit
from ..db import utcnow
from . import jobs as ops_jobs

STATUS_QUEUED = "QUEUED"
STATUS_CLAIMED = "CLAIMED"
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"


def enqueue_local_request(
    conn: sqlite3.Connection,
    *,
    state_id: int | None,
    district_id: int | None,
    department_filter: list[str] | None,
    created_by: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO extraction_requests
            (state_id, district_id, department_filter, status, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            state_id, district_id,
            json.dumps(department_filter) if department_filter else None,
            STATUS_QUEUED, created_by, utcnow(),
        ),
    )
    request_id = int(cur.lastrowid)
    audit.record(
        conn, action="extraction_request.queued", entity_type="extraction_request", entity_id=request_id,
        actor=created_by,
        detail={"state_id": state_id, "district_id": district_id, "department_filter": department_filter},
    )
    return request_id


def claim_next(conn: sqlite3.Connection, *, agent_key_id: int) -> sqlite3.Row | None:
    """Claims the oldest QUEUED request, if any. Optimistic concurrency: the
    UPDATE's WHERE clause re-checks status='QUEUED', so if two agents race
    for the same row, exactly one succeeds (rowcount 0 for the loser, who
    just tries again on its next poll)."""
    candidate = conn.execute(
        "SELECT id FROM extraction_requests WHERE status = ? ORDER BY id LIMIT 1", (STATUS_QUEUED,)
    ).fetchone()
    if candidate is None:
        return None
    request_id = int(candidate["id"])
    now = utcnow()
    cur = conn.execute(
        """
        UPDATE extraction_requests
           SET status = ?, claimed_by_agent_key_id = ?, claimed_at = ?
         WHERE id = ? AND status = ?
        """,
        (STATUS_CLAIMED, agent_key_id, now, request_id, STATUS_QUEUED),
    )
    if cur.rowcount == 0:
        return None  # lost the race to another agent
    return get_request(conn, request_id)


def _department_filter_of(row: sqlite3.Row) -> list[str] | None:
    return json.loads(row["department_filter"]) if row["department_filter"] else None


def claim_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """The JSON handed to the agent for a claimed request -- names, not ids
    (see module docstring)."""
    state_name = None
    district_name = None
    if row["state_id"] is not None:
        s = conn.execute("SELECT name FROM states WHERE id = ?", (row["state_id"],)).fetchone()
        state_name = s["name"] if s else None
    if row["district_id"] is not None:
        d = conn.execute("SELECT name FROM districts WHERE id = ?", (row["district_id"],)).fetchone()
        district_name = d["name"] if d else None
    return {
        "id": row["id"],
        "state_name": state_name,
        "district_name": district_name,
        "department_filter": _department_filter_of(row),
    }


def report_progress(
    conn: sqlite3.Connection, request_id: int, *,
    sources_total: int | None = None, sources_completed: int | None = None,
    documents_found: int | None = None, documents_downloaded: int | None = None,
    documents_parsed: int | None = None, documents_failed: int | None = None,
) -> None:
    row = get_request(conn, request_id)
    if row is None:
        raise LookupError(f"no extraction_request with id {request_id}")
    updates = {
        "sources_total": sources_total, "sources_completed": sources_completed,
        "documents_found": documents_found, "documents_downloaded": documents_downloaded,
        "documents_parsed": documents_parsed, "documents_failed": documents_failed,
    }
    set_clauses = [f"{k} = ?" for k, v in updates.items() if v is not None]
    params = [v for v in updates.values() if v is not None]
    if row["status"] == STATUS_CLAIMED:
        set_clauses.append("status = ?")
        params.append(STATUS_RUNNING)
        set_clauses.append("started_at = ?")
        params.append(utcnow())
    if not set_clauses:
        return
    params.append(request_id)
    conn.execute(f"UPDATE extraction_requests SET {', '.join(set_clauses)} WHERE id = ?", params)


def complete_request(
    conn: sqlite3.Connection, request_id: int, *, ok: bool, error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE extraction_requests SET status = ?, finished_at = ?, error = ? WHERE id = ?",
        (STATUS_COMPLETED if ok else STATUS_FAILED, utcnow(), error, request_id),
    )
    audit.record(
        conn, action="extraction_request.completed" if ok else "extraction_request.failed",
        entity_type="extraction_request", entity_id=request_id, actor="agent",
        detail={"error": error} if error else None,
    )


def get_request(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM extraction_requests WHERE id = ?", (request_id,)).fetchone()


def list_requests(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM extraction_requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def queue_size(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM extraction_requests WHERE status IN (?, ?)", (STATUS_QUEUED, STATUS_CLAIMED)
    ).fetchone()["n"]


def resolve_local_source_ids(
    conn: sqlite3.Connection, *, state_name: str | None, district_name: str | None,
    department_filter: list[str] | None,
) -> list[int]:
    """The agent-side counterpart to jobs.sources_in_scope, resolving by
    name against the agent's own local geography tables instead of by id
    (see module docstring for why). A name that was given but doesn't
    resolve locally returns an empty list rather than silently falling back
    to "no filter" -- an unrecognized scope must never widen to everything."""
    state_id = None
    if state_name:
        row = conn.execute("SELECT id FROM states WHERE name = ?", (state_name,)).fetchone()
        if row is None:
            return []
        state_id = int(row["id"])

    district_id = None
    if district_name:
        row = conn.execute("SELECT id FROM districts WHERE name = ?", (district_name,)).fetchone()
        if row is None:
            return []
        district_id = int(row["id"])

    rows = ops_jobs.sources_in_scope(
        conn, state_id=state_id, district_id=district_id, department_filter=department_filter,
    )
    return [int(r["id"]) for r in rows]


# ---------------------------------------------------------------------------
# In-process automatic extraction queue worker
# ---------------------------------------------------------------------------
_WORKER_THREAD: threading.Thread | None = None
_WORKER_LOCK = threading.Lock()
_WAKE_EVENT = threading.Event()


def start_auto_worker(settings: Settings) -> None:
    """Start in-process background worker daemon to poll and execute queued extraction requests."""
    global _WORKER_THREAD
    with _WORKER_LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            return
        _WORKER_THREAD = threading.Thread(
            target=_queue_worker_loop,
            args=(settings,),
            daemon=True,
            name="thirdeye-extraction-queue-worker",
        )
        _WORKER_THREAD.start()


def trigger_worker_now() -> None:
    """Signal the background worker to process immediately without waiting for poll interval."""
    _WAKE_EVENT.set()


def is_worker_running() -> bool:
    return _WORKER_THREAD is not None and _WORKER_THREAD.is_alive()


def _queue_worker_loop(settings: Settings) -> None:
    from ..fetching import HttpFetcher

    fetcher = HttpFetcher()
    try:
        while True:
            try:
                _process_queued_requests(settings, fetcher)
            except Exception:
                pass
            _WAKE_EVENT.wait(timeout=10.0)
            _WAKE_EVENT.clear()
    finally:
        fetcher.close()


def _process_queued_requests(settings: Settings, fetcher) -> None:
    from ..db import connect
    from ..pipeline import run_all, run_parsing

    conn = connect(settings.db_path)
    try:
        candidate = conn.execute(
            "SELECT id FROM extraction_requests WHERE status = ? ORDER BY id LIMIT 1",
            (STATUS_QUEUED,),
        ).fetchone()
        if candidate is None:
            # Also auto-parse any unparsed documents in the repository
            try:
                run_parsing(conn, settings, limit=100)
            except Exception:
                pass
            return

        request_id = int(candidate["id"])
        key_row = conn.execute(
            "SELECT id FROM agent_keys WHERE revoked_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        agent_key_id = int(key_row["id"]) if key_row else 1

        req = claim_next(conn, agent_key_id=agent_key_id)
        if req is None:
            return

        state_name = None
        district_name = None
        if req["state_id"] is not None:
            s = conn.execute("SELECT name FROM states WHERE id = ?", (req["state_id"],)).fetchone()
            state_name = s["name"] if s else None
        if req["district_id"] is not None:
            d = conn.execute("SELECT name FROM districts WHERE id = ?", (req["district_id"],)).fetchone()
            district_name = d["name"] if d else None

        dept_filter = _department_filter_of(req)
        source_ids = resolve_local_source_ids(
            conn, state_name=state_name, district_name=district_name, department_filter=dept_filter,
        )

        if not source_ids:
            complete_request(conn, request_id, ok=False, error="No sources in scope for requested filters")
            return

        report_progress(
            conn, request_id, sources_total=len(source_ids), sources_completed=0,
            documents_found=0, documents_downloaded=0, documents_parsed=0, documents_failed=0,
        )

        docs_found = docs_downloaded = docs_parsed = docs_failed = sources_completed = 0
        for sid in source_ids:
            try:
                report = run_all(conn, settings, fetcher, only_due=False, source_id=sid, max_pages=20, limit=1000)
                docs_found += report.new_documents
                docs_downloaded += report.download.succeeded
                docs_parsed += report.parse.succeeded
                docs_failed += report.download.failed + report.parse.failed
            except Exception as exc:
                docs_failed += 1

            sources_completed += 1
            report_progress(
                conn, request_id, sources_completed=sources_completed,
                documents_found=docs_found, documents_downloaded=docs_downloaded,
                documents_parsed=docs_parsed, documents_failed=docs_failed,
            )

        # Parse all remaining unparsed documents for this request's scope
        try:
            parse_report = run_parsing(conn, settings, limit=1000)
            docs_parsed += parse_report.succeeded
        except Exception:
            pass

        complete_request(conn, request_id, ok=True)
    except Exception as exc:
        try:
            complete_request(conn, request_id, ok=False, error=str(exc))
        except Exception:
            pass
    finally:
        conn.close()

