"""Module 12 -- System Health Center.

Every number here is read from state the app already tracks (source crawl
history, OCR availability, repository size, job queue depth) -- this is a
monitoring VIEW, not a new subsystem with its own polling daemon. Alerts are
computed at render time from thresholds, which is honest about what this POC
actually provides: a dashboard an operator checks, not a paging system.
"""

from __future__ import annotations

import sqlite3

from .. import repository
from ..extraction import ocr
from . import jobs as ops_jobs

# 5 GB default -- a reasonable ceiling for a single-machine POC repository.
# Override via Settings/environment if a deployment needs a different limit.
DEFAULT_STORAGE_THRESHOLD_BYTES = 5 * 1024 * 1024 * 1024

ALERT_SOURCE_DOWN = "source_down"
ALERT_CERTIFICATION_FAILURE = "certification_failure"
ALERT_OCR_FAILURE = "ocr_failure"
ALERT_STORAGE_THRESHOLD = "storage_threshold"


def source_availability(conn: sqlite3.Connection) -> dict:
    sources = conn.execute(
        "SELECT id, name, last_crawl_status, last_crawl_success_at, last_crawl_failure_at "
        "FROM sources WHERE active = 1"
    ).fetchall()
    down = [
        {"id": r["id"], "name": r["name"]}
        for r in sources
        if r["last_crawl_status"] == "error"
        or (r["last_crawl_failure_at"] and (r["last_crawl_success_at"] or "") < r["last_crawl_failure_at"])
    ]
    return {
        "total_active": len(sources),
        "healthy": len(sources) - len(down),
        "down": down,
    }


def source_health_table(conn: sqlite3.Connection) -> list[dict]:
    """Per-source health, not just the aggregate up/down count above.

    The health score is a plain, documented sum -- not a black box, since a
    number an operator can't reconstruct isn't actionable:
      +50 last crawl succeeded (or no crawl attempted yet, benefit of the doubt)
      +30 at least one document has ever been downloaded from this source
      +20 fewer than 20% of discovered documents were rejected

    Status bands: Healthy >=80, Warning 50-79, Critical 20-49, Offline <20
    (or the source has never been crawled at all, which is worth flagging
    even though it isn't technically a "failure").
    """
    rows = conn.execute(
        """
        SELECT s.id, s.name, s.priority, s.last_crawl_at, s.last_crawl_status,
               COUNT(DISTINCT dd.id) AS documents_found,
               COUNT(DISTINCT doc.id) AS documents_downloaded,
               COUNT(DISTINCT CASE WHEN dd.status = 'rejected' THEN dd.id END) AS failed_downloads,
               COUNT(DISTINCT CASE WHEN e.needs_ocr = 1 THEN e.id END) AS needs_ocr_count,
               COUNT(DISTINCT e.id) AS extraction_count
          FROM sources s
          LEFT JOIN discovered_documents dd ON dd.source_id = s.id
          LEFT JOIN documents doc ON doc.source_id = s.id
          LEFT JOIN extractions e ON e.document_id = doc.id
         WHERE s.active = 1
         GROUP BY s.id
         ORDER BY s.priority = 'Critical' DESC, s.priority = 'High' DESC, s.name
        """
    ).fetchall()

    table: list[dict] = []
    for r in rows:
        found = int(r["documents_found"])
        downloaded = int(r["documents_downloaded"])
        failed = int(r["failed_downloads"])
        reject_rate = (failed / found) if found else 0.0

        score = 0
        score += 50 if r["last_crawl_status"] in (None, "ok") else 0
        score += 30 if downloaded > 0 else 0
        score += 20 if reject_rate < 0.20 else 0

        if r["last_crawl_at"] is None:
            status = "Offline"
        elif score >= 80:
            status = "Healthy"
        elif score >= 50:
            status = "Warning"
        elif score >= 20:
            status = "Critical"
        else:
            status = "Offline"

        extraction_count = int(r["extraction_count"])
        table.append({
            "id": r["id"],
            "name": r["name"],
            "priority": r["priority"],
            "last_crawl_at": r["last_crawl_at"],
            "documents_found": found,
            "documents_downloaded": downloaded,
            "failed_downloads": failed,
            "needs_ocr_count": int(r["needs_ocr_count"]),
            "ocr_percent": round(100 * int(r["needs_ocr_count"]) / extraction_count, 1) if extraction_count else None,
            "health_score": score,
            "status": status,
        })
    return table


def recent_certification_failures(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT sc.*, s.name AS source_name FROM source_certifications sc
          JOIN sources s ON s.id = sc.source_id
         WHERE sc.result = 'FAILED'
         ORDER BY sc.id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def ocr_health(conn: sqlite3.Connection) -> dict:
    return {
        "available": ocr.is_available(),
        "languages": ocr.available_languages() if ocr.is_available() else [],
        "documents_needing_ocr": conn.execute(
            "SELECT COUNT(*) AS n FROM extractions WHERE needs_ocr = 1"
        ).fetchone()["n"],
    }


def storage_health(settings, conn: sqlite3.Connection, *, threshold_bytes: int = DEFAULT_STORAGE_THRESHOLD_BYTES) -> dict:
    stats = repository.stats(settings, conn)
    used = stats["total_bytes"]
    return {
        "used_bytes": used,
        "threshold_bytes": threshold_bytes,
        "percent_used": round(used / threshold_bytes * 100, 2) if threshold_bytes else 0.0,
        "over_threshold": used > threshold_bytes,
    }


def system_health(conn: sqlite3.Connection, settings) -> dict:
    availability = source_availability(conn)
    failures = recent_certification_failures(conn, limit=10)
    ocr_status = ocr_health(conn)
    storage = storage_health(settings, conn)
    queue_depth = ops_jobs.active_job_count(conn)

    alerts: list[dict] = []
    if availability["down"]:
        alerts.append({
            "type": ALERT_SOURCE_DOWN,
            "message": f"{len(availability['down'])} source(s) down: " + ", ".join(s["name"] for s in availability["down"]),
        })
    if failures:
        alerts.append({
            "type": ALERT_CERTIFICATION_FAILURE,
            "message": f"{len(failures)} recent certification failure(s)",
        })
    if not ocr_status["available"]:
        alerts.append({
            "type": ALERT_OCR_FAILURE,
            "message": "Tesseract OCR is not available -- scanned documents cannot be recovered",
        })
    if storage["over_threshold"]:
        alerts.append({
            "type": ALERT_STORAGE_THRESHOLD,
            "message": f"Repository storage at {storage['percent_used']}% of threshold",
        })

    return {
        "api_health": "ok",  # this response returning at all is the check
        "source_availability": availability,
        "certification_failures": failures,
        "ocr": ocr_status,
        "storage": storage,
        "processing_queue_depth": queue_depth,
        "alerts": alerts,
    }
