"""Module 6 -- Benchmark & Accuracy Engine.

Measures precision, recall and F1 per field against REAL human-verified
golden annotations only (governance rules 3 & 4). This is a different data
source from `goengine.benchmark`, Phase 1's CSV-driven harness: that module
scores synthetic fixtures for developer regression testing and is never used
for Phase 2 certification numbers. This module reads exclusively from
`golden_documents`/`golden_annotations`, which can only reference documents
that passed through the real acquisition pipeline.

Each document is re-extracted with the CURRENT extractor code before
scoring (idempotent -- a no-op if it already reflects the current version),
so a certification run always measures what the pipeline does today, not
whatever it did when the document was first parsed.

Field comparison reuses `goengine.benchmark.values_match`: the same
field-appropriate matching (GO numbers by digits+series, dates by parsed
value, budgets by rupee amount, districts by gazetteer, subjects by
containment/overlap) applies whether the ground truth came from a CSV
fixture or a human annotator.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from .. import audit
from ..benchmark import values_match
from ..db import utcnow
from ..extraction.metadata import extract_and_store, load_fields
from . import golden

SCORED_FIELDS = golden.SCORED_FIELDS

# Sample Accuracy Targets, Phase 2 blueprint Module 6.
TARGETS: dict[str, float] = {
    "go_number": 0.99,
    "go_date": 0.99,
    "department": 0.98,
    "subject": 0.95,
    "budget": 0.95,
    "district": 0.95,
    "scheme_name": 0.90,
}


@dataclass
class FieldStats:
    field_name: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def precision(self) -> float | None:
        denom = self.tp + self.fp
        return self.tp / denom if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.tp + self.fn
        return self.tp / denom if denom else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)

    @property
    def accuracy(self) -> float | None:
        total = self.tp + self.fp + self.fn + self.tn
        return (self.tp + self.tn) / total if total else None

    @property
    def support(self) -> int:
        """Documents where this field was scored at all."""
        return self.tp + self.fp + self.fn + self.tn

    def merge(self, other: "FieldStats") -> "FieldStats":
        return FieldStats(
            self.field_name, self.tp + other.tp, self.fp + other.fp,
            self.fn + other.fn, self.tn + other.tn,
        )

    def to_dict(self) -> dict:
        return {
            "tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
            "precision": self.precision, "recall": self.recall, "f1": self.f1,
            "accuracy": self.accuracy, "support": self.support,
            "target": TARGETS.get(self.field_name),
            "meets_target": (
                self.accuracy >= TARGETS[self.field_name]
                if self.accuracy is not None and self.field_name in TARGETS
                else None
            ),
        }


@dataclass
class Mismatch:
    document_id: int
    golden_document_id: int
    field_name: str
    expected: str | None
    actual: str | None
    kind: str  # wrong | missed | hallucinated
    department_bucket: str | None
    language: str | None
    # The machine field's own `method` tag (e.g. "GO_NUMBER_FULL+series@references"),
    # None when nothing was extracted. Module 7 uses the @region suffix to
    # tell "picked the wrong order's number from a citation" apart from a
    # plain extraction miss.
    method: str | None = None


@dataclass
class ConfidenceObservation:
    """One machine prediction's stated confidence vs. whether it was right.

    Only recorded when the machine actually asserted a value (a "missed" or
    correctly-silent field has no confidence to calibrate). Module 8 buckets
    these by confidence decile to check whether "92% confident" really does
    mean "right about 92% of the time".
    """

    document_id: int
    field_name: str
    confidence: float
    is_correct: bool


@dataclass
class CertificationBenchmarkResult:
    run_id: int
    documents_scored: int
    extractor_version: str
    overall: dict[str, FieldStats] = field(default_factory=dict)
    by_department: dict[str, dict[str, FieldStats]] = field(default_factory=dict)
    by_language: dict[str, dict[str, FieldStats]] = field(default_factory=dict)
    mismatches: list[Mismatch] = field(default_factory=list)
    observations: list[ConfidenceObservation] = field(default_factory=list)
    skipped_incomplete: int = 0

    @property
    def phase2_targets_met(self) -> bool:
        return all(
            stats.accuracy is not None and stats.accuracy >= TARGETS[name]
            for name, stats in self.overall.items()
            if stats.support
        )

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "documents_scored": self.documents_scored,
            "extractor_version": self.extractor_version,
            "phase2_targets_met": self.phase2_targets_met,
            "overall": {name: s.to_dict() for name, s in self.overall.items()},
            "by_department": {
                bucket: {name: s.to_dict() for name, s in fields.items()}
                for bucket, fields in self.by_department.items()
            },
            "by_language": {
                lang: {name: s.to_dict() for name, s in fields.items()}
                for lang, fields in self.by_language.items()
            },
            "mismatch_count": len(self.mismatches),
            "skipped_incomplete": self.skipped_incomplete,
        }


def _score_one(
    expected: str | None, actual: str | None, field_name: str
) -> tuple[str, bool]:
    """Returns (outcome, is_tp) where outcome in {tp, fp_wrong, fn_missed, fp_hallucinated, tn}."""
    if expected is not None and actual is not None:
        return ("tp", True) if values_match(field_name, expected, actual) else ("wrong", False)
    if expected is not None and actual is None:
        return "missed", False
    if expected is None and actual is not None:
        return "hallucinated", False
    return "tn", False


def run_certification_benchmark(
    conn: sqlite3.Connection, *, actor: str = audit.SYSTEM_ACTOR
) -> CertificationBenchmarkResult:
    """Score every fully-annotated golden document against current extraction."""
    documents = golden.list_golden_documents(conn)
    overall = {name: FieldStats(name) for name in SCORED_FIELDS}
    by_department: dict[str, dict[str, FieldStats]] = {}
    by_language: dict[str, dict[str, FieldStats]] = {}
    mismatches: list[Mismatch] = []
    observations: list[ConfidenceObservation] = []
    scored_count = 0
    skipped = 0

    for doc_row in documents:
        golden_id = int(doc_row["id"])
        document_id = int(doc_row["document_id"])
        annotations = golden.get_annotations(conn, golden_id)
        if not set(SCORED_FIELDS) <= set(annotations):
            skipped += 1
            continue

        extraction_row = conn.execute(
            "SELECT id FROM extractions WHERE document_id = ? ORDER BY id DESC LIMIT 1",
            (document_id,),
        ).fetchone()
        if extraction_row is None:
            skipped += 1
            continue

        # Always measure the CURRENT extractor, not whatever ran originally.
        record_id = extract_and_store(conn, int(extraction_row["id"]), actor=actor)
        machine_fields = load_fields(conn, record_id)

        bucket = doc_row["department_bucket"] or "unknown"
        language = doc_row["language"] or "unknown"
        by_department.setdefault(bucket, {name: FieldStats(name) for name in SCORED_FIELDS})
        by_language.setdefault(language, {name: FieldStats(name) for name in SCORED_FIELDS})

        for field_name in SCORED_FIELDS:
            expected = annotations[field_name]["value"]
            actual_row = machine_fields.get(field_name)
            actual = actual_row["normalized_value"] if actual_row else None

            outcome, _ = _score_one(expected, actual, field_name)
            stats = overall[field_name]
            dept_stats = by_department[bucket][field_name]
            lang_stats = by_language[language][field_name]

            for target in (stats, dept_stats, lang_stats):
                if outcome == "tp":
                    target.tp += 1
                elif outcome == "wrong":
                    target.fp += 1
                    target.fn += 1
                elif outcome == "missed":
                    target.fn += 1
                elif outcome == "hallucinated":
                    target.fp += 1
                else:
                    target.tn += 1

            if outcome in ("wrong", "missed", "hallucinated"):
                mismatches.append(
                    Mismatch(
                        document_id=document_id, golden_document_id=golden_id,
                        field_name=field_name, expected=expected, actual=actual,
                        kind=outcome, department_bucket=bucket, language=language,
                        method=actual_row["method"] if actual_row else None,
                    )
                )

            # A confidence observation exists only where the machine actually
            # asserted something -- "tp" (right) or "wrong"/"hallucinated"
            # (confidently asserted, incorrectly). "missed"/"tn" made no
            # claim, so there is nothing to calibrate.
            if actual_row is not None:
                observations.append(
                    ConfidenceObservation(
                        document_id=document_id, field_name=field_name,
                        confidence=float(actual_row["confidence"]),
                        is_correct=outcome == "tp",
                    )
                )

        scored_count += 1

    result = CertificationBenchmarkResult(
        run_id=0, documents_scored=scored_count, extractor_version="",
        overall=overall, by_department=by_department, by_language=by_language,
        mismatches=mismatches, observations=observations, skipped_incomplete=skipped,
    )

    from ..extraction.metadata import EXTRACTOR_VERSION

    result.extractor_version = EXTRACTOR_VERSION
    run_at = utcnow()
    cur = conn.execute(
        """
        INSERT INTO certification_benchmark_runs (run_at, extractor_version, documents_scored, summary)
        VALUES (?, ?, ?, ?)
        """,
        (run_at, EXTRACTOR_VERSION, scored_count, json.dumps(result.to_dict(), ensure_ascii=False)),
    )
    result.run_id = int(cur.lastrowid)

    audit.record(
        conn,
        action="certification.benchmark_run",
        entity_type="certification_benchmark_run",
        entity_id=result.run_id,
        actor=actor,
        detail={
            "documents_scored": scored_count,
            "skipped_incomplete": skipped,
            "phase2_targets_met": result.phase2_targets_met,
        },
    )
    return result


def latest_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM certification_benchmark_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def run_history(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, run_at, extractor_version, documents_scored FROM certification_benchmark_runs "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
