"""GO Canonical Identifier Blueprint v1.0.

Standardizes GO identity ahead of expanding extraction coverage beyond the
current pilot departments (see the blueprint's own stated objective). This
module computes four *derived* identity columns on `go_records` from the
two fields extraction already produces (`go_number`, `go_date`) and the
record's own source -- it never extracts anything new from a document.

Citizen-facing: `go_identifier`, e.g. "GO41/2022" -- exactly the blueprint's
format, dropping the series (Ms/Rt/D/...) for readability.

Internal: `canonical_go_id`, e.g. "HFW-MS-GO41/2022" -- keeps the series
specifically because the citizen-facing format cannot: "G.O.(Ms) No.41/2022"
and "G.O.(Rt) No.41/2022" from the same department in the same year are two
different orders, and the blueprint's own success criterion #2 ("internal
IDs remain unique") requires this module to actually guarantee it, not just
format nicely. SQLite can't add a UNIQUE constraint via ALTER TABLE without
rebuilding the table (a real risk on the live production database this app
already handles carefully elsewhere) -- so uniqueness is guaranteed here, at
write time, by checking for a collision and appending the record id as a
last-resort disambiguator when one is found.

All four columns are left `None` when `go_number`/`go_date` are missing or
unparseable (e.g. a record approved with the missing-core-field override) --
display and search fall back to the raw extracted `go_number` text in that
case. Nothing here is ever fabricated.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date

# sources.department -> short code. Keyed on the clean, canonical name
# already stored per source in the registry (registry.py's SEED_SOURCES),
# not on the noisier OCR'd `department` go_field -- avoids fuzzy matching
# entirely. Covers today's 20 configured departments; anything else (the 3
# non-department sources: GO Portal, Government Portal, Gazette) falls back
# to a generic prefix in _department_code(), still uniqueness-checked the
# same way.
DEPARTMENT_ABBREVIATIONS: dict[str, str] = {
    "Health and Family Welfare": "HFW",
    "School Education": "SE",
    "Rural Development and Panchayat Raj": "RD",
    "Public Works": "PWD",
    "Agriculture and Farmers Welfare": "AGR",
    "Highways and Minor Ports": "HWY",
    "Water Resources": "WR",
    "Municipal Administration and Water Supply": "MAWS",
    "Revenue and Disaster Management": "REV",
    "Finance": "FIN",
    "Housing and Urban Development": "HUD",
    "Transport": "TRA",
    "Industries, Investment Promotion and Commerce": "IND",
    "Environment, Climate Change and Forests": "ENV",
    "Tourism, Culture and Religious Endowments": "TOU",
    "Labour Welfare and Skill Development": "LAB",
    "Social Welfare and Women Empowerment": "SW",
    "Animal Husbandry, Dairying, Fisheries and Fishermen Welfare": "AHF",
    "Energy": "ENE",
    "Co-operation, Food and Consumer Protection": "COOP",
}

_GENERIC_DEPARTMENT_CODE = "GO"

# Mirrors extraction/patterns.py::GO_SERIES_LABELS' vocabulary (Ms/Rt/D/P/2D)
# rather than reinventing it -- "NS" (no series) covers extract_go_number's
# ORDER_NUMBER_LOOSE fallback, which never captures a series.
_SERIES_CODES: dict[str, str] = {"Ms": "MS", "Rt": "RT", "D": "D", "P": "P", "2D": "2D"}
_NO_SERIES_CODE = "NS"

# Matches the normalized go_number values extract_go_number actually
# produces: "G.O.(Ms) No.41", "G.O. No.41" (no series), or the weaker
# "Order No.41" fallback.
_GO_NUMBER_TEXT_RE = re.compile(
    r"""
    ^(?:G\.O\.(?:\((?P<series>Ms|Rt|D|P|2D)\))?\s*|Order\s+)
    No\.(?P<number>[0-9]{1,5})$
    """,
    re.VERBOSE,
)


def parse_go_number_text(raw: str | None) -> tuple[int, str] | None:
    """(number, series_code) from an already-extracted go_number value, or
    None if it doesn't parse. Never guesses -- an unrecognized format (e.g.
    a future extractor change) simply yields no identity, not a wrong one."""
    if not raw:
        return None
    match = _GO_NUMBER_TEXT_RE.match(raw.strip())
    if match is None:
        return None
    series = match.group("series")
    code = _SERIES_CODES[series] if series else _NO_SERIES_CODE
    return int(match.group("number")), code


def _department_code(department: str | None) -> str:
    if department and department in DEPARTMENT_ABBREVIATIONS:
        return DEPARTMENT_ABBREVIATIONS[department]
    return _GENERIC_DEPARTMENT_CODE


def _parse_year(go_date_raw: str | None) -> int | None:
    if not go_date_raw:
        return None
    try:
        return date.fromisoformat(go_date_raw[:10]).year
    except ValueError:
        return None


def compute_identity(conn: sqlite3.Connection, record_id: int) -> None:
    """Recomputes and stores the four identity columns for one record from
    its current go_number/go_date fields and its source's department. Safe
    to call repeatedly -- e.g. once when a record is first created, and
    again whenever go_number or go_date is corrected."""
    row = conn.execute(
        """
        SELECT
            gn.normalized_value AS go_number_raw,
            gd.normalized_value AS go_date_raw,
            s.department AS department
          FROM go_records r
          JOIN sources s ON s.id = r.source_id
          LEFT JOIN go_fields gn ON gn.record_id = r.id AND gn.field_name = 'go_number' AND gn.superseded_by IS NULL
          LEFT JOIN go_fields gd ON gd.record_id = r.id AND gd.field_name = 'go_date' AND gd.superseded_by IS NULL
         WHERE r.id = ?
        """,
        (record_id,),
    ).fetchone()
    if row is None:
        return

    parsed = parse_go_number_text(row["go_number_raw"])
    year = _parse_year(row["go_date_raw"])

    if parsed is None or year is None:
        conn.execute(
            """
            UPDATE go_records
               SET go_number_raw = ?, go_number_numeric = NULL, go_year = NULL,
                   go_identifier = NULL, canonical_go_id = NULL
             WHERE id = ?
            """,
            (row["go_number_raw"], record_id),
        )
        return

    number, series_code = parsed
    identifier = f"GO{number}/{year}"
    dept_code = _department_code(row["department"])
    canonical = f"{dept_code}-{series_code}-{identifier}"

    # Uniqueness is guaranteed here, at write time, rather than assumed from
    # the formatting scheme -- SQLite can't add a UNIQUE constraint via
    # ALTER TABLE without a table rebuild, so this is the actual guarantee
    # behind the blueprint's "internal IDs remain unique" success criterion.
    collision = conn.execute(
        "SELECT id FROM go_records WHERE canonical_go_id = ? AND id != ?",
        (canonical, record_id),
    ).fetchone()
    if collision is not None:
        canonical = f"{canonical}-R{record_id}"

    conn.execute(
        """
        UPDATE go_records
           SET go_number_raw = ?, go_number_numeric = ?, go_year = ?,
               go_identifier = ?, canonical_go_id = ?
         WHERE id = ?
        """,
        (row["go_number_raw"], number, year, identifier, canonical, record_id),
    )


def backfill_all(conn: sqlite3.Connection) -> int:
    """Computes identity for every existing record that doesn't have one
    yet -- the blueprint's "backward compatibility" requirement. Idempotent:
    a record already carrying a go_identifier is left untouched, so this is
    safe to call on every application boot (see db.py::init_db) without
    ever redoing work on a redeploy."""
    rows = conn.execute("SELECT id FROM go_records WHERE go_identifier IS NULL").fetchall()
    for row in rows:
        compute_identity(conn, int(row["id"]))
    return len(rows)
