"""Module 6 -- Metadata Extraction Engine.

Every value this module emits is a `FieldCandidate` carrying the page it came
from, the surrounding source text, character offsets and a confidence score.
There is deliberately no code path that produces a bare value: if a field
cannot be evidenced, it is simply absent, and the workbench shows it as
missing rather than guessing.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable

from .. import audit
from ..db import utcnow
from . import patterns as P
from .text import PageText, load_pages

EXTRACTOR_VERSION = "rules-1.0"

CORE_FIELDS = ("go_number", "go_date", "department", "subject")
OPTIONAL_FIELDS = ("budget", "district", "scheme_name")
ALL_FIELDS = CORE_FIELDS + OPTIONAL_FIELDS

# Characters of context kept around a match as displayable evidence.
EVIDENCE_WINDOW = 120


@dataclass
class FieldCandidate:
    field_name: str
    value: str
    normalized_value: str
    source_page: int
    source_text: str
    char_start: int
    char_end: int
    confidence: float
    method: str


@dataclass
class ExtractedMetadata:
    fields: dict[str, FieldCandidate] = field(default_factory=dict)
    candidates: list[FieldCandidate] = field(default_factory=list)

    def value(self, name: str) -> str | None:
        found = self.fields.get(name)
        return found.normalized_value if found else None

    @property
    def missing_core_fields(self) -> list[str]:
        return [name for name in CORE_FIELDS if name not in self.fields]


def _evidence(text: str, start: int, end: int) -> str:
    left = max(0, start - EVIDENCE_WINDOW // 2)
    right = min(len(text), end + EVIDENCE_WINDOW)
    snippet = text[left:right].strip()
    return re.sub(r"\s+", " ", snippet)


def _page_weight(page_number: int) -> float:
    """Header fields live on page 1; a later-page match is likelier a citation
    of some *other* order, so it is scored down rather than trusted equally."""
    if page_number == 1:
        return 1.0
    if page_number == 2:
        return 0.75
    return 0.5


HEADER = "header"
REFERENCES = "references"
BODY = "body"

# How much a match in each region is trusted, per field. The "Read:" block
# cites other orders, so a GO number or date found there is almost certainly
# not this order's own -- the single biggest source of false positives on real
# GOs, and the reason region beats raw pattern precision in the scoring.
REGION_WEIGHTS: dict[str, dict[str, float]] = {
    "go_number": {HEADER: 1.0, REFERENCES: 0.22, BODY: 0.55},
    "go_date": {HEADER: 1.0, REFERENCES: 0.22, BODY: 0.55},
    "department": {HEADER: 1.0, REFERENCES: 0.45, BODY: 0.70},
    "subject": {HEADER: 1.0, REFERENCES: 0.80, BODY: 0.80},
}
DEFAULT_REGION_WEIGHT = {HEADER: 1.0, REFERENCES: 0.85, BODY: 1.0}


def page_regions(text: str) -> tuple[int | None, int | None]:
    """Offsets where the Read block and the ORDER body begin, if present."""
    read_match = P.READ_MARKER.search(text)
    order_match = P.ORDER_MARKER.search(text)
    return (
        read_match.start() if read_match else None,
        order_match.start() if order_match else None,
    )


def region_at(position: int, bounds: tuple[int | None, int | None], default: str = HEADER) -> str:
    read_start, order_start = bounds
    if order_start is not None and position >= order_start:
        return BODY
    if read_start is not None and position >= read_start:
        return REFERENCES
    return default


def _region_weight(field_name: str, region: str) -> float:
    return REGION_WEIGHTS.get(field_name, DEFAULT_REGION_WEIGHT)[region]


def _document_region_state(pages: list[PageText]) -> dict[int, tuple[tuple[int | None, int | None], str]]:
    """Per-page (read_start, order_start) bounds plus the region carried in
    from the end of the previous page.

    The "Read:" citation block and the operative ORDER: text routinely run
    on past a single OCR page, but the literal marker word only appears on
    the page where it starts -- every later page of that same block has no
    marker of its own, so without carrying the region forward it silently
    fell back to HEADER (full trust) for the rest of the document. That let
    OCR-garbled reference numbers on page 2+ outscore the real header value
    on page 1, confirmed on a real GO where a garbled Read-block digit run
    briefly outranked the true "No.300" header match.
    """
    state: dict[int, tuple[tuple[int | None, int | None], str]] = {}
    carry = HEADER
    for page in pages:
        bounds = page_regions(page.text)
        state[page.page_number] = (bounds, carry)
        read_start, order_start = bounds
        if order_start is not None:
            carry = BODY
        elif read_start is not None:
            carry = REFERENCES
    return state


def _positional_weight(
    field_name: str,
    page: PageText,
    position: int,
    region_state: dict[int, tuple[tuple[int | None, int | None], str]],
) -> tuple[float, str]:
    """Combined page + region multiplier, and the region name for the audit."""
    bounds, carry = region_state[page.page_number]
    region = region_at(position, bounds, default=carry)
    return _page_weight(page.page_number) * _region_weight(field_name, region), region


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------
def extract_go_number(pages: Iterable[PageText]) -> list[FieldCandidate]:
    pages = list(pages)
    region_state = _document_region_state(pages)
    found: list[FieldCandidate] = []
    for page in pages:
        for match in P.GO_NUMBER_FULL.finditer(page.text):
            number = match.group("number")
            series_raw = match.group("series") or match.group("series2") or ""
            series = P.GO_SERIES_LABELS.get(series_raw.upper(), series_raw)
            value = match.group(0).strip()
            normalized = f"G.O.({series}) No.{number}" if series else f"G.O. No.{number}"

            # An explicit series is the strongest signal that this is the
            # order's own number rather than a reference to another one.
            base = 0.98 if series else 0.86
            weight, region = _positional_weight("go_number", page, match.start(), region_state)
            found.append(
                FieldCandidate(
                    field_name="go_number",
                    value=value,
                    normalized_value=normalized,
                    source_page=page.page_number,
                    source_text=_evidence(page.text, match.start(), match.end()),
                    char_start=match.start(),
                    char_end=match.end(),
                    confidence=round(base * weight, 4),
                    method=f"GO_NUMBER_FULL{'+series' if series else ''}@{region}",
                )
            )
        if not found:
            for match in P.ORDER_NUMBER_LOOSE.finditer(page.text):
                weight, region = _positional_weight("go_number", page, match.start(), region_state)
                found.append(
                    FieldCandidate(
                        field_name="go_number",
                        value=match.group(0).strip(),
                        normalized_value=f"Order No.{match.group('number')}",
                        source_page=page.page_number,
                        source_text=_evidence(page.text, match.start(), match.end()),
                        char_start=match.start(),
                        char_end=match.end(),
                        confidence=round(0.55 * weight, 4),
                        method=f"ORDER_NUMBER_LOOSE@{region}",
                    )
                )
    return found


def _build_date(day: int, month: int, year: int) -> date | None:
    if year < 100:
        year += 2000 if year < 70 else 1900
    try:
        return date(year, month, day)
    except ValueError:
        return None


def extract_go_date(pages: Iterable[PageText]) -> list[FieldCandidate]:
    pages = list(pages)
    region_state = _document_region_state(pages)
    found: list[FieldCandidate] = []
    specs: tuple[tuple[re.Pattern[str], str, float, bool], ...] = (
        (P.DATE_NUMERIC_LABELLED, "DATE_NUMERIC_LABELLED", 0.97, False),
        (P.DATE_TEXTUAL_LABELLED, "DATE_TEXTUAL_LABELLED", 0.97, True),
        (P.DATE_NUMERIC_BARE, "DATE_NUMERIC_BARE", 0.62, False),
        (P.DATE_TEXTUAL_BARE, "DATE_TEXTUAL_BARE", 0.62, True),
    )
    for page in pages:
        for pattern, method, base, textual_month in specs:
            for match in pattern.finditer(page.text):
                day = int(match.group("day"))
                month_raw = match.group("month")
                month = P.MONTHS[month_raw.lower()] if textual_month else int(month_raw)
                parsed = _build_date(day, month, int(match.group("year")))
                if parsed is None:
                    continue
                # A GO cannot be dated in the future; that is a parse error
                # (usually a day/month swap) rather than a real value.
                if parsed > date.today():
                    continue
                weight, region = _positional_weight("go_date", page, match.start(), region_state)
                found.append(
                    FieldCandidate(
                        field_name="go_date",
                        value=match.group(0).strip(),
                        normalized_value=parsed.isoformat(),
                        source_page=page.page_number,
                        source_text=_evidence(page.text, match.start(), match.end()),
                        char_start=match.start(),
                        char_end=match.end(),
                        confidence=round(base * weight, 4),
                        method=f"{method}@{region}",
                    )
                )
    return found


def extract_department(pages: Iterable[PageText]) -> list[FieldCandidate]:
    pages = list(pages)
    region_state = _document_region_state(pages)
    found: list[FieldCandidate] = []
    for page in pages:
        for match in P.DEPARTMENT_HEADING.finditer(page.text):
            normalized = P.normalize_department(match.group("name"))
            if not normalized:
                continue
            base = 0.98 if P.is_known_department(normalized) else 0.80
            weight, region = _positional_weight("department", page, match.start(), region_state)
            found.append(
                FieldCandidate(
                    field_name="department",
                    value=re.sub(r"\s+", " ", match.group(0)).strip(),
                    normalized_value=normalized,
                    source_page=page.page_number,
                    source_text=_evidence(page.text, match.start(), match.end()),
                    char_start=match.start(),
                    char_end=match.end(),
                    confidence=round(base * weight, 4),
                    method=f"DEPARTMENT_HEADING@{region}",
                )
            )
        for match in P.DEPARTMENT_IN_ABSTRACT.finditer(page.text):
            normalized = P.normalize_department(match.group("name"))
            if not normalized or not P.is_known_department(normalized):
                continue
            weight, region = _positional_weight("department", page, match.start(), region_state)
            found.append(
                FieldCandidate(
                    field_name="department",
                    value=re.sub(r"\s+", " ", match.group(0)).strip(),
                    normalized_value=normalized,
                    source_page=page.page_number,
                    source_text=_evidence(page.text, match.start(), match.end()),
                    char_start=match.start(),
                    char_end=match.end(),
                    confidence=round(0.90 * weight, 4),
                    method=f"DEPARTMENT_IN_ABSTRACT@{region}",
                )
            )
    return found


def extract_subject(pages: Iterable[PageText]) -> list[FieldCandidate]:
    found: list[FieldCandidate] = []
    for page in pages:
        match = P.ABSTRACT_BLOCK.search(page.text)
        if match:
            body = re.sub(r"\s+", " ", match.group("body")).strip(" -–—\n")
            if body:
                found.append(
                    FieldCandidate(
                        field_name="subject",
                        value=body,
                        normalized_value=body,
                        source_page=page.page_number,
                        source_text=_evidence(page.text, match.start("body"), match.end("body")),
                        char_start=match.start("body"),
                        char_end=match.end("body"),
                        confidence=round(0.95 * _page_weight(page.page_number), 4),
                        method="ABSTRACT_BLOCK",
                    )
                )
                continue

        # No ABSTRACT label: fall back to the paragraph closing with
        # "Orders issued", which is the abstract in practice.
        tail = P.ORDERS_ISSUED_TAIL.search(page.text)
        if tail:
            start = page.text.rfind("\n\n", 0, tail.start())
            start = 0 if start == -1 else start + 2
            body = re.sub(r"\s+", " ", page.text[start : tail.end()]).strip(" -–—")
            if 15 <= len(body) <= 1200:
                found.append(
                    FieldCandidate(
                        field_name="subject",
                        value=body,
                        normalized_value=body,
                        source_page=page.page_number,
                        source_text=_evidence(page.text, start, tail.end()),
                        char_start=start,
                        char_end=tail.end(),
                        confidence=round(0.72 * _page_weight(page.page_number), 4),
                        method="ORDERS_ISSUED_TAIL",
                    )
                )
    return found


def extract_budget(pages: Iterable[PageText]) -> list[FieldCandidate]:
    found: list[FieldCandidate] = []
    for page in pages:
        for match in P.MONEY_NUMERIC.finditer(page.text):
            raw_amount = match.group("amount").replace(",", "")
            try:
                amount = float(raw_amount)
            except ValueError:
                continue
            unit = (match.group("unit") or "").lower()
            multiplier = P.UNIT_MULTIPLIERS.get(unit, 1)
            total = amount * multiplier
            if total <= 0:
                continue
            # The sanctioned amount is usually the largest figure in the order;
            # ranking by magnitude beats ranking by position here.
            magnitude_bonus = min(len(str(int(total))) / 12.0, 0.25)
            found.append(
                FieldCandidate(
                    field_name="budget",
                    value=match.group(0).strip(),
                    normalized_value=f"{total:.2f}",
                    source_page=page.page_number,
                    source_text=_evidence(page.text, match.start(), match.end()),
                    char_start=match.start(),
                    char_end=match.end(),
                    confidence=round(min(0.70 + magnitude_bonus, 0.95), 4),
                    method="MONEY_NUMERIC" + (f"+{unit}" if unit else ""),
                )
            )
    return found


def extract_district(pages: Iterable[PageText]) -> list[FieldCandidate]:
    found: list[FieldCandidate] = []
    names = list(P.TN_DISTRICTS) + list(P.DISTRICT_ALIASES)
    district_re = re.compile(
        r"\b(" + "|".join(sorted((re.escape(n) for n in names), key=len, reverse=True)) + r")\b"
        r"(?P<suffix>\s+District)?",
        re.IGNORECASE,
    )
    counts: dict[str, int] = {}
    first: dict[str, tuple[PageText, re.Match[str]]] = {}
    for page in pages:
        for match in district_re.finditer(page.text):
            canonical = P.normalize_district(match.group(1))
            if canonical is None:
                continue
            counts[canonical] = counts.get(canonical, 0) + 1
            if canonical not in first:
                first[canonical] = (page, match)

    for canonical, (page, match) in first.items():
        # Repeated mentions and an explicit "X District" both raise confidence.
        repeat_bonus = min((counts[canonical] - 1) * 0.05, 0.15)
        suffix_bonus = 0.10 if match.group("suffix") else 0.0
        found.append(
            FieldCandidate(
                field_name="district",
                value=match.group(0).strip(),
                normalized_value=canonical,
                source_page=page.page_number,
                source_text=_evidence(page.text, match.start(), match.end()),
                char_start=match.start(),
                char_end=match.end(),
                confidence=round(min(0.70 + repeat_bonus + suffix_bonus, 0.95), 4),
                method="TN_DISTRICT_GAZETTEER",
            )
        )
    return found


def extract_scheme_name(pages: Iterable[PageText]) -> list[FieldCandidate]:
    found: list[FieldCandidate] = []
    for page in pages:
        for match in P.SCHEME_NAME.finditer(page.text):
            value = (match.group("name") or match.group("name2") or "").strip()
            # Strip connectives the pattern may have picked up ahead of the name.
            while True:
                stripped = P.SCHEME_LEADING_FILLER.sub("", value)
                if stripped == value:
                    break
                value = stripped
            if len(value) < 6:
                continue
            keyword = bool(
                re.search(r"\b(Scheme|Thittam|Mission|Yojana|Programme|Project)\b", value, re.I)
            )
            found.append(
                FieldCandidate(
                    field_name="scheme_name",
                    value=value,
                    normalized_value=re.sub(r"\s+", " ", value),
                    source_page=page.page_number,
                    source_text=_evidence(page.text, match.start(), match.end()),
                    char_start=match.start(),
                    char_end=match.end(),
                    confidence=round(0.80 if keyword else 0.60, 4),
                    method="SCHEME_NAME" + ("+keyword" if keyword else "+quoted"),
                )
            )
    return found


EXTRACTORS: dict[str, Callable[[Iterable[PageText]], list[FieldCandidate]]] = {
    "go_number": extract_go_number,
    "go_date": extract_go_date,
    "department": extract_department,
    "subject": extract_subject,
    "budget": extract_budget,
    "district": extract_district,
    "scheme_name": extract_scheme_name,
}


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def _agreement_adjust(candidates: list[FieldCandidate]) -> list[FieldCandidate]:
    """Reward corroboration, penalise contradiction.

    Several matches normalizing to the same value is evidence the reading is
    right. Several matches disagreeing means the extractor is guessing, and
    the winner's confidence should reflect that so it surfaces for review.
    """
    if not candidates:
        return candidates
    tally: dict[str, int] = {}
    strongest: dict[str, float] = {}
    for candidate in candidates:
        key = candidate.normalized_value
        tally[key] = tally.get(key, 0) + 1
        strongest[key] = max(strongest.get(key, 0.0), candidate.confidence)

    adjusted: list[FieldCandidate] = []
    for candidate in candidates:
        confidence = candidate.confidence
        support = tally[candidate.normalized_value]
        if support > 1:
            confidence = min(confidence + min((support - 1) * 0.02, 0.06), 0.99)

        # Penalise by how CLOSE the best rival reading is, not by how many
        # rivals exist. A GO that cites five other orders is not ambiguous;
        # one with a single equally-plausible alternative genuinely is.
        rival = max(
            (score for value, score in strongest.items() if value != candidate.normalized_value),
            default=0.0,
        )
        if rival > 0 and candidate.confidence > 0:
            ratio = min(rival / candidate.confidence, 1.0)
            confidence *= 1.0 - 0.15 * ratio

        adjusted.append(
            FieldCandidate(**{**candidate.__dict__, "confidence": round(confidence, 4)})
        )
    return adjusted


def select_best(candidates: list[FieldCandidate]) -> FieldCandidate | None:
    if not candidates:
        return None
    ranked = _agreement_adjust(candidates)
    # Ties break toward the earliest page, then the earliest position.
    return max(
        ranked,
        key=lambda c: (c.confidence, -c.source_page, -c.char_start),
    )


def extract_metadata(pages: list[PageText]) -> ExtractedMetadata:
    """Run every field extractor over the page text."""
    result = ExtractedMetadata()
    for field_name, extractor in EXTRACTORS.items():
        candidates = _agreement_adjust(extractor(pages))
        result.candidates.extend(candidates)
        best = select_best(candidates)
        if best is not None:
            result.fields[field_name] = best
    return result


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def extract_and_store(
    conn: sqlite3.Connection,
    extraction_id: int,
    *,
    actor: str = audit.SYSTEM_ACTOR,
) -> int:
    """Build a GO record with evidence-bound fields. Returns the record id."""
    row = conn.execute(
        """
        SELECT e.document_id, d.source_id
          FROM extractions e
          JOIN documents d ON d.id = e.document_id
         WHERE e.id = ?
        """,
        (extraction_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no extraction with id {extraction_id}")

    existing = conn.execute(
        "SELECT id FROM go_records WHERE extraction_id = ? AND extractor_version = ?",
        (extraction_id, EXTRACTOR_VERSION),
    ).fetchone()
    if existing is not None:
        return int(existing["id"])

    pages = load_pages(conn, extraction_id)
    metadata = extract_metadata(pages)

    cur = conn.execute(
        """
        INSERT INTO go_records
            (extraction_id, document_id, source_id, extractor_version, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (
            extraction_id,
            int(row["document_id"]),
            int(row["source_id"]),
            EXTRACTOR_VERSION,
            utcnow(),
        ),
    )
    record_id = int(cur.lastrowid)

    now = utcnow()
    for candidate in metadata.fields.values():
        conn.execute(
            """
            INSERT INTO go_fields
                (record_id, field_name, value, normalized_value, source_page, source_text,
                 char_start, char_end, confidence, method, origin, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'extracted', ?, ?)
            """,
            (
                record_id,
                candidate.field_name,
                candidate.value,
                candidate.normalized_value,
                candidate.source_page,
                candidate.source_text,
                candidate.char_start,
                candidate.char_end,
                candidate.confidence,
                candidate.method,
                now,
                actor,
            ),
        )

    from .. import go_identity

    go_identity.compute_identity(conn, record_id)

    winners = {(c.field_name, c.normalized_value) for c in metadata.fields.values()}
    for candidate in metadata.candidates:
        if (candidate.field_name, candidate.normalized_value) in winners:
            continue
        conn.execute(
            """
            INSERT INTO go_field_candidates
                (record_id, field_name, value, source_page, source_text, confidence, method)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                candidate.field_name,
                candidate.value,
                candidate.source_page,
                candidate.source_text,
                candidate.confidence,
                candidate.method,
            ),
        )

    audit.record(
        conn,
        action="metadata.extracted",
        entity_type="go_record",
        entity_id=record_id,
        actor=actor,
        detail={
            "extraction_id": extraction_id,
            "extractor_version": EXTRACTOR_VERSION,
            "fields_found": sorted(metadata.fields),
            "missing_core_fields": metadata.missing_core_fields,
        },
    )
    return record_id


def load_fields(conn: sqlite3.Connection, record_id: int) -> dict[str, sqlite3.Row]:
    """Current (non-superseded) field rows, keyed by field name."""
    rows = conn.execute(
        """
        SELECT * FROM go_fields
         WHERE record_id = ? AND superseded_by IS NULL
         ORDER BY field_name, id
        """,
        (record_id,),
    ).fetchall()
    return {r["field_name"]: r for r in rows}
