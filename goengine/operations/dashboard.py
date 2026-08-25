"""Module 9 -- Operations Dashboard.

Pure composition: every number here comes from a function Modules 1-8
already built and already test. This module's only job is assembling them
into one executive view -- no new computation, no new source of truth.
"""

from __future__ import annotations

import json
import sqlite3

from .. import repository, review
from ..certification.sources import certification_summary
from . import publication as ops_publication


def operations_summary(conn: sqlite3.Connection, settings) -> dict:
    active_states = conn.execute(
        "SELECT COUNT(*) AS n FROM states WHERE active = 1 AND status = 'ACTIVE'"
    ).fetchone()["n"]
    total_states = conn.execute("SELECT COUNT(*) AS n FROM states").fetchone()["n"]

    active_districts = conn.execute(
        "SELECT COUNT(*) AS n FROM districts WHERE status IN ('CERTIFIED', 'PUBLISHED')"
    ).fetchone()["n"]
    total_districts = conn.execute("SELECT COUNT(*) AS n FROM districts").fetchone()["n"]

    active_departments = conn.execute(
        "SELECT COUNT(*) AS n FROM departments WHERE active = 1"
    ).fetchone()["n"]

    source_cert = certification_summary(conn)
    active_sources = conn.execute("SELECT COUNT(*) AS n FROM sources WHERE active = 1").fetchone()["n"]
    total_sources = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]

    repo_stats = repository.stats(settings, conn)
    review_counts = review.counts_by_status(conn)
    approved_records = review_counts[review.STATUS_APPROVED]

    registered_citizens = conn.execute(
        "SELECT COUNT(*) AS n FROM citizen_users WHERE active = 1"
    ).fetchone()["n"]

    latest_run = conn.execute(
        "SELECT * FROM certification_benchmark_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    accuracy_score = None
    if latest_run is not None:
        summary = json.loads(latest_run["summary"])
        accuracies = [
            s["accuracy"] for s in summary.get("overall", {}).values()
            if s.get("accuracy") is not None and s.get("support")
        ]
        accuracy_score = round(sum(accuracies) / len(accuracies), 4) if accuracies else None

    return {
        "active_states": active_states,
        "total_states": total_states,
        "active_districts": active_districts,
        "total_districts": total_districts,
        "active_departments": active_departments,
        "certified_sources": source_cert["CERTIFIED"],
        "active_sources": active_sources,
        "total_sources": total_sources,
        "documents_processed": repo_stats["documents"],
        "approved_records": approved_records,
        "registered_citizens": registered_citizens,
        "documents_requiring_review": review_counts["pending"],
        "accuracy_score": accuracy_score,
        "publication_coverage": ops_publication.publication_coverage(conn),
        "latest_benchmark_run_id": latest_run["id"] if latest_run else None,
    }
