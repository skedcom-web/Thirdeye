"""Source-scoping for extraction runs.

Cloud-run extraction (this server crawling tn.gov.in directly) was removed:
Render's network is blocked at the TCP level by TN government hosts, so it
could never actually succeed in production. The local extraction agent (see
cli.py's `agent-daemon` command and operations/extraction_queue.py) is the
only extraction path now -- it runs on a machine with unblocked network and
syncs results back. `sources_in_scope` is shared infrastructure both the
(removed) cloud runner and the local agent's `extraction_queue.
resolve_local_source_ids` used to pick which sources a request covers.
"""

from __future__ import annotations

import sqlite3

from . import geography


def sources_in_scope(
    conn: sqlite3.Connection, *, state_id: int | None, district_id: int | None,
    department_filter: list[str] | None,
) -> list[sqlite3.Row]:
    if district_id is not None:
        rows = geography.applicable_sources(conn, district_id)
    elif state_id is not None:
        rows = conn.execute(
            "SELECT * FROM sources WHERE active = 1 AND (state_id = ? OR state_id IS NULL)", (state_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM sources WHERE active = 1").fetchall()

    if department_filter:
        # Matches sources.department directly -- e.g. "Health and Family
        # Welfare" -- not the 4-value content bucket (certification/
        # categorize.py's ALL_BUCKETS), which is a separate concern
        # (classifying a document's *content* into a broad public-facing
        # bucket, not selecting which sources to crawl).
        rows = [r for r in rows if r["department"] in department_filter]
    return rows


def cleanup_expired_evidence(conn: sqlite3.Connection) -> int:
    """Removes request-level crawl evidence older than configured retention
    days. Called from cli.py's local-agent request handling after a batch
    completes (previously ran after each cloud job)."""
    row = conn.execute("SELECT value FROM system_settings WHERE key = 'diagnostics_retention_days'").fetchone()
    days = int(row["value"]) if row else 30

    from datetime import datetime, timezone, timedelta

    from .. import audit

    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")

    cur = conn.execute("DELETE FROM crawl_evidences WHERE timestamp < ?", (threshold,))
    deleted = cur.rowcount
    if deleted > 0:
        audit.record(
            conn,
            action="diagnostics.cleanup",
            entity_type="system",
            entity_id=0,
            actor="system",
            detail={"deleted_records": deleted, "threshold": threshold}
        )
    return deleted
