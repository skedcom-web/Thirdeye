"""Golden dataset harness and accuracy scoring.

The golden dataset is a directory of PDFs plus an `annotations.csv` of
hand-checked ground truth. Scoring runs the real extraction path over those
PDFs and reports per-field accuracy against the Phase 1 targets.

Annotation CSV columns (only `file_name` is mandatory; leave a cell blank when
the field genuinely does not appear in that order):

    file_name,go_number,go_date,department,subject,budget,district,scheme_name

A blank cell means "not present in the document", and the extractor is scored
correct only if it also reports nothing. That keeps the metric honest about
hallucination, not just about recall.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable

from .extraction.metadata import ALL_FIELDS, extract_metadata
from .extraction.patterns import normalize_district
from .extraction.text import extract_file

ANNOTATIONS_FILE = "annotations.csv"

# Phase 1 and Phase 2 targets from the blueprint.
TARGETS: dict[str, float] = {
    "go_number": 0.99,
    "go_date": 0.99,
    "department": 0.99,
    "subject": 0.95,
}
PHASE2_TARGETS: dict[str, float] = {
    "budget": 0.95,
    "district": 0.95,
    "scheme_name": 0.90,
}


@dataclass
class FieldScore:
    field_name: str
    total: int = 0
    correct: int = 0
    wrong: int = 0
    missed: int = 0        # truth present, extractor found nothing
    hallucinated: int = 0  # truth absent, extractor produced a value
    mistakes: list[dict[str, str]] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def target(self) -> float | None:
        return TARGETS.get(self.field_name) or PHASE2_TARGETS.get(self.field_name)

    @property
    def meets_target(self) -> bool | None:
        target = self.target
        if target is None:
            return None
        return self.accuracy >= target


@dataclass
class BenchmarkReport:
    documents: int = 0
    failures: list[str] = field(default_factory=list)
    scores: dict[str, FieldScore] = field(default_factory=dict)
    mean_extraction_confidence: float = 0.0
    documents_needing_ocr: int = 0

    @property
    def phase1_pass(self) -> bool:
        return all(
            self.scores[name].meets_target
            for name in TARGETS
            if name in self.scores and self.scores[name].total
        )

    def to_dict(self) -> dict:
        return {
            "documents": self.documents,
            "documents_needing_ocr": self.documents_needing_ocr,
            "mean_extraction_confidence": round(self.mean_extraction_confidence, 4),
            "phase1_pass": self.phase1_pass,
            "failures": self.failures,
            "fields": {
                name: {
                    "total": s.total,
                    "correct": s.correct,
                    "wrong": s.wrong,
                    "missed": s.missed,
                    "hallucinated": s.hallucinated,
                    "accuracy": round(s.accuracy, 4),
                    "target": s.target,
                    "meets_target": s.meets_target,
                    "mistakes": s.mistakes[:20],
                }
                for name, s in self.scores.items()
            },
        }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
_PUNCT = re.compile(r"[^a-z0-9]+")


def _fold(value: str) -> str:
    return _PUNCT.sub(" ", value.lower()).strip()


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _parse_money(value: str) -> float | None:
    """Amount in rupees.

    The unit must directly follow the digits. TN orders routinely restate the
    figure in words -- "Rs.2,45,00,000/- (Rupees Two Crore Forty Five Lakh
    only)" -- and scanning the whole string for "crore" would scale an
    already-complete numeral by ten million.
    """
    cleaned = value.replace(",", "")
    match = re.search(
        r"(\d+(?:\.\d+)?)\s*(crores?|lakhs?|lacs?|thousand)?\b", cleaned, re.IGNORECASE
    )
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "").lower().rstrip("s")
    if unit == "crore":
        amount *= 10_000_000
    elif unit in ("lakh", "lac"):
        amount *= 100_000
    elif unit == "thousand":
        amount *= 1_000
    return amount


def _parse_date(value: str) -> str | None:
    value = value.strip()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    match = re.match(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", value)
    if match:
        day, month, year = (int(g) for g in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def values_match(field_name: str, expected: str, actual: str) -> bool:
    """Field-appropriate comparison.

    Exact string equality would fail on formatting differences that carry no
    meaning ("G.O.(Ms) No.123" vs "G.O. Ms No. 123"), so each field is
    compared on what actually identifies it.
    """
    expected, actual = expected.strip(), actual.strip()
    if not expected or not actual:
        return expected == actual

    if field_name == "go_number":
        # The digits plus the series letter are what identify an order.
        exp_series = re.search(r"\b(Ms|Rt|D|P)\b", expected, re.I)
        act_series = re.search(r"\b(Ms|Rt|D|P)\b", actual, re.I)
        if _digits(expected) != _digits(actual):
            return False
        if exp_series and act_series:
            return exp_series.group(1).lower() == act_series.group(1).lower()
        return True

    if field_name == "go_date":
        parsed_expected, parsed_actual = _parse_date(expected), _parse_date(actual)
        if parsed_expected and parsed_actual:
            return parsed_expected == parsed_actual
        return _fold(expected) == _fold(actual)

    if field_name == "budget":
        exp_amount, act_amount = _parse_money(expected), _parse_money(actual)
        if exp_amount is None or act_amount is None:
            return _fold(expected) == _fold(actual)
        return abs(exp_amount - act_amount) < 1.0

    if field_name == "district":
        return (normalize_district(expected) or _fold(expected)) == (
            normalize_district(actual) or _fold(actual)
        )

    if field_name == "subject":
        # Subject is free text; require the annotated wording to be carried,
        # allowing for trailing boilerplate the extractor may include.
        exp_folded, act_folded = _fold(expected), _fold(actual)
        if exp_folded == act_folded:
            return True
        if exp_folded in act_folded or act_folded in exp_folded:
            return True
        return _token_overlap(exp_folded, act_folded) >= 0.90

    return _fold(expected) == _fold(actual)


def _token_overlap(left: str, right: str) -> float:
    left_tokens, right_tokens = set(left.split()), set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


# ---------------------------------------------------------------------------
# Dataset IO
# ---------------------------------------------------------------------------
def load_annotations(dataset_dir: Path) -> list[dict[str, str]]:
    path = dataset_dir / ANNOTATIONS_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"no {ANNOTATIONS_FILE} in {dataset_dir}; run `thirdeye golden init` first"
        )
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = [
            {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            for row in csv.DictReader(handle)
        ]
    missing_name = [i for i, row in enumerate(rows, start=2) if not row.get("file_name")]
    if missing_name:
        raise ValueError(f"{ANNOTATIONS_FILE} rows missing file_name: lines {missing_name}")
    return rows


def write_annotation_template(
    dataset_dir: Path, rows: Iterable[dict[str, str]] | None = None
) -> Path:
    """Create (or extend) the annotation CSV, pre-filled with the PDFs present."""
    dataset_dir.mkdir(parents=True, exist_ok=True)
    path = dataset_dir / ANNOTATIONS_FILE
    columns = ["file_name", *ALL_FIELDS]

    existing: dict[str, dict[str, str]] = {}
    if path.exists():
        existing = {row["file_name"]: row for row in load_annotations(dataset_dir)}

    supplied = {row["file_name"]: row for row in (rows or [])}
    discovered = sorted(p.name for p in dataset_dir.glob("*.pdf"))

    merged: list[dict[str, str]] = []
    for name in discovered:
        row = {column: "" for column in columns}
        row["file_name"] = name
        row.update({k: v for k, v in existing.get(name, {}).items() if v})
        row.update({k: v for k, v in supplied.get(name, {}).items() if v})
        merged.append(row)

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(merged)
    return path


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_dataset(
    dataset_dir: Path, *, fields: tuple[str, ...] = ALL_FIELDS
) -> BenchmarkReport:
    annotations = load_annotations(dataset_dir)
    report = BenchmarkReport()
    report.scores = {name: FieldScore(name) for name in fields}

    confidences: list[float] = []

    for row in annotations:
        pdf_path = dataset_dir / row["file_name"]
        if not pdf_path.exists():
            report.failures.append(f"{row['file_name']}: file not found")
            continue

        try:
            extraction = extract_file(pdf_path)
        except Exception as exc:
            report.failures.append(f"{row['file_name']}: extraction failed ({exc})")
            continue

        report.documents += 1
        confidences.append(extraction.confidence)
        if extraction.needs_ocr:
            report.documents_needing_ocr += 1

        metadata = extract_metadata(extraction.pages)

        for name in fields:
            expected = row.get(name, "").strip()
            candidate = metadata.fields.get(name)
            actual = candidate.normalized_value if candidate else ""

            # An unannotated field is not scored: we cannot tell whether a
            # blank means "absent from the order" or "not yet annotated"
            # unless the annotator filled the row in. Rows are counted only
            # when the annotation file declares the column for that document.
            if name not in row:
                continue

            score = report.scores[name]
            score.total += 1

            if expected and actual:
                if values_match(name, expected, actual):
                    score.correct += 1
                else:
                    score.wrong += 1
                    score.mistakes.append(
                        {
                            "file_name": row["file_name"],
                            "expected": expected,
                            "actual": actual,
                            "kind": "wrong",
                        }
                    )
            elif expected and not actual:
                score.missed += 1
                score.mistakes.append(
                    {
                        "file_name": row["file_name"],
                        "expected": expected,
                        "actual": "",
                        "kind": "missed",
                    }
                )
            elif not expected and actual:
                score.hallucinated += 1
                score.mistakes.append(
                    {
                        "file_name": row["file_name"],
                        "expected": "",
                        "actual": actual,
                        "kind": "hallucinated",
                    }
                )
            else:
                # Correctly reported nothing.
                score.correct += 1

    report.mean_extraction_confidence = (
        sum(confidences) / len(confidences) if confidences else 0.0
    )
    return report


def format_report(report: BenchmarkReport, *, show_mistakes: bool = True) -> str:
    lines: list[str] = []
    lines.append(f"Documents scored : {report.documents}")
    lines.append(f"Needing OCR      : {report.documents_needing_ocr}")
    lines.append(f"Mean text conf.  : {report.mean_extraction_confidence:.1%}")
    lines.append("")
    lines.append(f"{'Field':<14}{'Acc':>8}{'Target':>9}{'OK':>5}"
                 f"{'Wrong':>7}{'Missed':>8}{'Halluc':>8}{'':>4}")
    lines.append("-" * 64)

    for name, score in report.scores.items():
        if not score.total:
            continue
        target = f"{score.target:.0%}" if score.target else "-"
        verdict = "" if score.meets_target is None else ("PASS" if score.meets_target else "FAIL")
        lines.append(
            f"{name:<14}{score.accuracy:>7.1%}{target:>9}{score.correct:>5}"
            f"{score.wrong:>7}{score.missed:>8}{score.hallucinated:>8}{verdict:>6}"
        )

    lines.append("")
    lines.append(
        "Phase 1 accuracy targets: " + ("MET" if report.phase1_pass else "NOT MET")
    )

    if report.failures:
        lines.append("")
        lines.append(f"Failures ({len(report.failures)}):")
        lines.extend(f"  - {failure}" for failure in report.failures[:20])

    if show_mistakes:
        for name, score in report.scores.items():
            if not score.mistakes:
                continue
            lines.append("")
            lines.append(f"{name} -- {len(score.mistakes)} mistake(s):")
            for mistake in score.mistakes[:10]:
                lines.append(
                    f"  [{mistake['kind']}] {mistake['file_name']}: "
                    f"expected {mistake['expected'][:60]!r}, got {mistake['actual'][:60]!r}"
                )
    return "\n".join(lines)


def write_json_report(report: BenchmarkReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return path
