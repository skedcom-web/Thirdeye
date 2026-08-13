"""Module 7 -- Failure Intelligence Engine.

Every mismatch Module 6 finds is classified into a root-cause category and
persisted permanently -- governance rule 5 ("every failure must be
recorded") is structural here too: `extraction_failures` has no delete
trigger of its own, but nothing in this codebase ever issues a DELETE
against it, and the categorization always runs as part of a benchmark run
rather than being optional.

Classification is a priority ladder, most likely root cause first: a
scanned document's failures are blamed on OCR before anything else, a
Tamil-dominant document's failures are blamed on language coverage next
(Phase 1's patterns are English-oriented), then a specific extractor
confusion (picked a cited order over the order's own number/date), then a
table-layout cause, and only then the generic per-field bucket.
"""

from __future__ import annotations

import sqlite3
from collections import Counter

from .. import audit
from ..db import utcnow
from . import categorize
from .benchmark import Mismatch

FAILURE_OCR = "ocr_failure"
FAILURE_BUDGET = "budget_failure"
FAILURE_DISTRICT = "district_failure"
FAILURE_TAMIL = "tamil_parsing_failure"
FAILURE_REFERENCE = "reference_misclassification"
FAILURE_TABLE = "table_extraction_failure"
FAILURE_HALLUCINATION = "hallucination"
FAILURE_OTHER = "other"

ALL_FAILURE_TYPES = (
    FAILURE_OCR, FAILURE_BUDGET, FAILURE_DISTRICT, FAILURE_TAMIL,
    FAILURE_REFERENCE, FAILURE_TABLE, FAILURE_HALLUCINATION, FAILURE_OTHER,
)

_REFERENCE_ELIGIBLE_FIELDS = ("go_number", "go_date")
_TABLE_ELIGIBLE_FIELDS = ("budget", "district")


def classify(mismatch: Mismatch, category_row: sqlite3.Row | None) -> str:
    if mismatch.kind == "hallucinated":
        return FAILURE_HALLUCINATION

    if category_row is not None and category_row["text_type"] == "scanned":
        return FAILURE_OCR

    if mismatch.language == "tamil":
        return FAILURE_TAMIL

    if mismatch.field_name in _REFERENCE_ELIGIBLE_FIELDS and mismatch.method and "@references" in mismatch.method:
        return FAILURE_REFERENCE

    if (
        category_row is not None
        and category_row["table_heavy"]
        and mismatch.field_name in _TABLE_ELIGIBLE_FIELDS
    ):
        return FAILURE_TABLE

    if mismatch.field_name == "budget":
        return FAILURE_BUDGET
    if mismatch.field_name == "district":
        return FAILURE_DISTRICT
    return FAILURE_OTHER


def record_failures(
    conn: sqlite3.Connection,
    benchmark_run_id: int,
    mismatches: list[Mismatch],
    *,
    actor: str = audit.SYSTEM_ACTOR,
) -> int:
    """Classify and persist every mismatch from a benchmark run. Returns count."""
    now = utcnow()
    counts: Counter[str] = Counter()
    for mismatch in mismatches:
        category_row = categorize.get_category(conn, mismatch.document_id)
        failure_type = classify(mismatch, category_row)
        counts[failure_type] += 1

        conn.execute(
            """
            INSERT INTO extraction_failures
                (benchmark_run_id, document_id, field_name, expected_value, actual_value,
                 failure_type, department_bucket, language, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                benchmark_run_id, mismatch.document_id, mismatch.field_name,
                mismatch.expected, mismatch.actual, failure_type,
                mismatch.department_bucket, mismatch.language, now,
            ),
        )

    if mismatches:
        audit.record(
            conn,
            action="failures.recorded",
            entity_type="certification_benchmark_run",
            entity_id=benchmark_run_id,
            actor=actor,
            detail={"count": len(mismatches), "by_type": dict(counts)},
        )
    return len(mismatches)


def top_failure_types(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT failure_type, COUNT(*) AS n
          FROM extraction_failures
         GROUP BY failure_type
         ORDER BY n DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def failure_trend(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Failure count per benchmark run, most recent last -- a trend line."""
    return conn.execute(
        """
        SELECT r.id AS run_id, r.run_at, r.documents_scored, COUNT(f.id) AS failure_count
          FROM certification_benchmark_runs r
          LEFT JOIN extraction_failures f ON f.benchmark_run_id = r.id
         GROUP BY r.id
         ORDER BY r.id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()[::-1]


def department_failure_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT COALESCE(department_bucket, 'unknown') AS department_bucket, COUNT(*) AS n
          FROM extraction_failures
         GROUP BY department_bucket
         ORDER BY n DESC
        """
    ).fetchall()


def language_failure_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT COALESCE(language, 'unknown') AS language, COUNT(*) AS n
          FROM extraction_failures
         GROUP BY language
         ORDER BY n DESC
        """
    ).fetchall()


def list_failures(
    conn: sqlite3.Connection,
    *,
    failure_type: str | None = None,
    field_name: str | None = None,
    department_bucket: str | None = None,
    language: str | None = None,
    benchmark_run_id: int | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list = []
    for column, value in (
        ("failure_type", failure_type), ("field_name", field_name),
        ("department_bucket", department_bucket), ("language", language),
        ("benchmark_run_id", benchmark_run_id),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    return conn.execute(
        f"SELECT * FROM extraction_failures {where} ORDER BY id DESC LIMIT ?", params
    ).fetchall()
