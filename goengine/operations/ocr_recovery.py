"""Phase 4.1 -- OCR Coverage Recovery Program.

Root cause (evidence-first investigation of 1,552 production records
missing a GO number): the local extraction agent's own sync loop
(cli.py's _run_claimed_request) uploads a document to the server
immediately after DOWNLOAD, before local parsing/OCR has run --
_sync_documents() then marks it `agent_synced_at`, permanently excluding it
from every later sync call in that same function, even though local
parsing (with real OCR, since the agent's machine has Tesseract -- the
server does not) runs moments later and produces real page text. That real
OCR result is computed but never transmitted. Confirmed against the real
local agent database: 1,494 documents have an ocr_runs row whose `ran_at`
postdates their `agent_synced_at`.

This module has two genuinely distinct halves, because the two sides of
this system run against different databases:

- Agent-side (local sqlite, real Tesseract, real OCR already computed):
  `find_unsynced_ocr_gaps()` / `reset_for_resync()`. Used by cli.py to fix
  the bug going forward (called before every sync in the normal request
  loop) and to recover already-broken documents (called by the new
  `ocr_recovery` request kind).
- Server-side (Turso, no Tesseract, never runs OCR itself):
  `pending_recovery_summary()` / `department_breakdown()`. Read-only
  reporting for the OCR Recovery Dashboard -- it reports what the local
  agent still needs to deliver; it never runs OCR itself.
"""

from __future__ import annotations

import sqlite3


# ---------------------------------------------------------------------------
# Agent-side: local sqlite database with real OCR already computed
# ---------------------------------------------------------------------------
def find_unsynced_ocr_gaps(conn: sqlite3.Connection, *, source_ids: list[int] | None = None) -> list[int]:
    """Document ids whose most recent local OCR run completed AFTER the
    document's last recorded sync -- the sync-ordering bug's exact
    signature. These documents have real OCR text sitting in this local
    database that the server has never received."""
    source_clause = ""
    params: list = []
    if source_ids is not None:
        if not source_ids:
            return []
        source_clause = f" AND d.source_id IN ({','.join('?' * len(source_ids))})"
        params.extend(source_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT d.id
          FROM documents d
          JOIN extractions e ON e.document_id = d.id
          JOIN ocr_runs orun ON orun.extraction_id = e.id
         WHERE d.agent_synced_at IS NOT NULL
           AND orun.ran_at > d.agent_synced_at
           {source_clause}
        """,
        params,
    ).fetchall()
    return [int(r["id"]) for r in rows]


def reset_for_resync(conn: sqlite3.Connection, document_ids: list[int]) -> int:
    """Clears agent_synced_at for the given documents so the very next
    _sync_documents() call re-pushes them -- this time with their real,
    already-computed OCR pages included. Never touches OCR data itself,
    only sync bookkeeping."""
    if not document_ids:
        return 0
    placeholders = ",".join("?" * len(document_ids))
    cur = conn.execute(
        f"UPDATE documents SET agent_synced_at = NULL WHERE id IN ({placeholders})", document_ids
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Server-side: read-only reporting for the OCR Recovery Dashboard
# ---------------------------------------------------------------------------
def _affected_records(conn: sqlite3.Connection, *, department: str | None = None) -> list[dict]:
    """Pending records: needs_ocr=1, an extraction.ocr_skipped audit event
    was recorded (OCR was skipped server-side -- never attempted-and-failed,
    just never attempted because Tesseract isn't installed here), and no
    ocr_runs row exists yet (the local agent's real OCR hasn't reached this
    record). Bulk queries only -- no per-record round trips."""
    dept_clause = " AND s.department = ?" if department else ""
    dept_params = (department,) if department else ()
    rows = conn.execute(
        f"""
        SELECT r.id AS record_id, r.extraction_id, s.department AS department
          FROM go_records r
          JOIN sources s ON s.id = r.source_id
          JOIN extractions e ON e.id = r.extraction_id
         WHERE e.needs_ocr = 1{dept_clause}
           AND NOT EXISTS (SELECT 1 FROM ocr_runs o WHERE o.extraction_id = r.extraction_id)
        """,
        dept_params,
    ).fetchall()
    if not rows:
        return []

    extraction_ids = sorted({int(r["extraction_id"]) for r in rows})
    skipped: set[int] = set()
    CHUNK = 150
    for i in range(0, len(extraction_ids), CHUNK):
        chunk = extraction_ids[i : i + CHUNK]
        placeholders = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"""
            SELECT DISTINCT entity_id FROM audit_log
             WHERE action = 'extraction.ocr_skipped' AND entity_type = 'extraction'
               AND entity_id IN ({placeholders})
            """,
            chunk,
        ):
            skipped.add(int(r["entity_id"]))

    return [
        {"record_id": int(r["record_id"]), "extraction_id": int(r["extraction_id"]), "department": r["department"]}
        for r in rows
        if int(r["extraction_id"]) in skipped
    ]


def pending_recovery_summary(conn: sqlite3.Connection) -> dict:
    """Overall counts for the dashboard's headline KPIs."""
    pending = _affected_records(conn)
    recovered_today_row = conn.execute(
        """
        SELECT COUNT(DISTINCT extraction_id) AS n FROM ocr_runs
         WHERE source = 'local_agent' AND substr(ran_at, 1, 10) = date('now')
        """
    ).fetchone()
    recovered_total_row = conn.execute(
        "SELECT COUNT(DISTINCT extraction_id) AS n FROM ocr_runs WHERE source = 'local_agent'"
    ).fetchone()
    recovered_total = int(recovered_total_row["n"])
    total_ever_needing_ocr = conn.execute(
        "SELECT COUNT(*) AS n FROM extractions WHERE needs_ocr = 1 OR id IN (SELECT extraction_id FROM ocr_runs)"
    ).fetchone()["n"]
    denominator = recovered_total + len(pending)
    success_rate = (recovered_total / denominator * 100.0) if denominator else 0.0
    return {
        "total_pending": len(pending),
        "recovered_today": int(recovered_today_row["n"]),
        "recovered_total": recovered_total,
        "recovery_success_rate_pct": round(success_rate, 1),
        "total_ever_needing_ocr": int(total_ever_needing_ocr),
    }


def department_breakdown(conn: sqlite3.Connection) -> list[dict]:
    """Top-impact departments, ranked by pending count."""
    pending = _affected_records(conn)
    counts: dict[str, int] = {}
    for r in pending:
        counts[r["department"]] = counts.get(r["department"], 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return [{"department": d, "pending": n} for d, n in ranked]


def pending_records_for_department(conn: sqlite3.Connection, department: str) -> list[dict]:
    return _affected_records(conn, department=department)
