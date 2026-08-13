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
