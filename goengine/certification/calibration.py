"""Module 8 -- Extraction Confidence Calibration.

Principle: internal confidence estimates must be benchmarked against ground
truth accuracy. A field the extractor calls "99% confident" should be right
about 99% of the time -- if it's actually right 60% of the time, the
confidence score is not measuring what it claims to, and a reviewer relying
on it to skip low-risk fields would be misled.

Buckets every confidence observation from a benchmark run (Module 6) into
deciles and compares each bucket's STATED mean confidence to its ACTUAL
accuracy. The gap is the calibration error for that bucket.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .benchmark import ConfidenceObservation

BUCKET_WIDTH = 0.1
BUCKET_EDGES: tuple[float, ...] = tuple(round(i * BUCKET_WIDTH, 1) for i in range(11))  # 0.0..1.0


@dataclass
class CalibrationBucket:
    field_name: str
    bucket_low: float
    bucket_high: float
    predictions_count: int
    correct_count: int
    mean_stated_confidence: float
    actual_accuracy: float

    @property
    def calibration_gap(self) -> float:
        """Positive: the model undersells itself. Negative: overconfident --
        the more actionable direction, since a reviewer trusting a high
        stated confidence would be trusting a field that's wrong more often
        than the number suggests."""
        return round(self.actual_accuracy - self.mean_stated_confidence, 4)

    def to_dict(self) -> dict:
        return {
            "bucket": f"{self.bucket_low:.1f}-{self.bucket_high:.1f}",
            "predictions_count": self.predictions_count,
            "correct_count": self.correct_count,
            "mean_stated_confidence": self.mean_stated_confidence,
            "actual_accuracy": self.actual_accuracy,
            "calibration_gap": self.calibration_gap,
        }


def _bucket_for(confidence: float) -> tuple[float, float]:
    confidence = min(max(confidence, 0.0), 1.0)
    index = min(int(confidence / BUCKET_WIDTH), len(BUCKET_EDGES) - 2)
    return BUCKET_EDGES[index], BUCKET_EDGES[index + 1]


def compute_calibration(
    observations: list[ConfidenceObservation], *, by_field: bool = True
) -> list[CalibrationBucket]:
    """Bucket observations by confidence decile, per field (or pooled)."""
    groups: dict[tuple[str, float, float], list[ConfidenceObservation]] = {}
    for obs in observations:
        low, high = _bucket_for(obs.confidence)
        key = (obs.field_name if by_field else "*", low, high)
        groups.setdefault(key, []).append(obs)

    buckets: list[CalibrationBucket] = []
    for (field_name, low, high), items in sorted(groups.items()):
        correct = sum(1 for o in items if o.is_correct)
        mean_conf = sum(o.confidence for o in items) / len(items)
        buckets.append(
            CalibrationBucket(
                field_name=field_name, bucket_low=low, bucket_high=high,
                predictions_count=len(items), correct_count=correct,
                mean_stated_confidence=round(mean_conf, 4),
                actual_accuracy=round(correct / len(items), 4),
            )
        )
    return buckets


def persist_calibration(
    conn: sqlite3.Connection, benchmark_run_id: int, buckets: list[CalibrationBucket]
) -> None:
    conn.executemany(
        """
        INSERT INTO calibration_snapshots
            (benchmark_run_id, field_name, bucket_low, bucket_high, predictions_count,
             correct_count, mean_stated_confidence, actual_accuracy, calibration_gap)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                benchmark_run_id, b.field_name, b.bucket_low, b.bucket_high,
                b.predictions_count, b.correct_count, b.mean_stated_confidence,
                b.actual_accuracy, b.calibration_gap,
            )
            for b in buckets
        ],
    )


def overall_calibration_error(buckets: list[CalibrationBucket]) -> float | None:
    """Prediction-count-weighted mean absolute calibration gap -- one number
    summarizing "how far off is stated confidence from reality, on average"."""
    total = sum(b.predictions_count for b in buckets)
    if not total:
        return None
    weighted = sum(abs(b.calibration_gap) * b.predictions_count for b in buckets)
    return round(weighted / total, 4)


def latest_calibration(conn: sqlite3.Connection, *, field_name: str | None = None) -> list[sqlite3.Row]:
    run = conn.execute(
        "SELECT id FROM certification_benchmark_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if run is None:
        return []
    clause = "AND field_name = ?" if field_name else ""
    params: list = [int(run["id"])]
    if field_name:
        params.append(field_name)
    return conn.execute(
        f"""
        SELECT * FROM calibration_snapshots
         WHERE benchmark_run_id = ? {clause}
         ORDER BY field_name, bucket_low
        """,
        params,
    ).fetchall()


def calibration_for_run(conn: sqlite3.Connection, benchmark_run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM calibration_snapshots WHERE benchmark_run_id = ? ORDER BY field_name, bucket_low",
        (benchmark_run_id,),
    ).fetchall()
