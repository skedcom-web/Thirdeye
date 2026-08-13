"""Phase 2 -- certification, categorization, OCR, benchmarking, failures, calibration."""

from __future__ import annotations

import sqlite3

from .. import audit
from . import calibration
from .benchmark import CertificationBenchmarkResult, run_certification_benchmark
from .failures import record_failures


def run_full_certification(
    conn: sqlite3.Connection, *, actor: str = audit.SYSTEM_ACTOR
) -> CertificationBenchmarkResult:
    """Modules 6, 7 and 8 in one pass: score the golden set, persist the
    result, classify and record every mismatch as a failure, and snapshot
    confidence calibration -- all against the same benchmark run."""
    result = run_certification_benchmark(conn, actor=actor)
    record_failures(conn, result.run_id, result.mismatches, actor=actor)
    buckets = calibration.compute_calibration(result.observations)
    calibration.persist_calibration(conn, result.run_id, buckets)
    return result
