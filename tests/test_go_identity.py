"""GO Canonical Identifier Blueprint v1.0 -- goengine/go_identity.py."""

from __future__ import annotations

import sqlite3

import pytest

from goengine import go_identity, review
from goengine.db import utcnow
from goengine.pipeline import run_all


# ---------------------------------------------------------------------------
# Pure parsing -- no DB needed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("G.O.(Ms) No.41", (41, "MS")),
        ("G.O.(Rt) No.456", (456, "RT")),
        ("G.O.(D) No.7", (7, "D")),
        ("G.O.(P) No.9", (9, "P")),
        ("G.O.(2D) No.12", (12, "2D")),
        ("G.O. No.212", (212, "NS")),
        ("Order No.41", (41, "NS")),
    ],
)
def test_parse_go_number_text_recognizes_every_series(raw, expected):
    assert go_identity.parse_go_number_text(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "not a go number", "G.O.(XY) No.1", "No.41"])
def test_parse_go_number_text_returns_none_for_unrecognized_input(raw):
    assert go_identity.parse_go_number_text(raw) is None


# ---------------------------------------------------------------------------
# compute_identity -- built against a manually inserted record so every
# input (department, series, missing fields) can be controlled precisely.
# ---------------------------------------------------------------------------
_counter = 0


def _insert_record(
    conn: sqlite3.Connection,
    *,
    department: str,
    go_number: str | None,
    go_date: str | None,
) -> int:
    global _counter
    _counter += 1
    now = utcnow()
    unique = f"{now}-{_counter}"
    source_id = conn.execute(
        "SELECT id FROM sources WHERE department = ?", (department,)
    ).fetchone()
    if source_id is None:
        cur = conn.execute(
            """
            INSERT INTO sources (name, department, url, host, source_type, adapter, created_at)
            VALUES (?, ?, ?, ?, 'department_site', 'tn_go_portal', ?)
            """,
            (f"{department} test source", department, f"https://tn.gov.in/{department}", "tn.gov.in", now),
        )
        source_id = cur.lastrowid
    else:
        source_id = source_id["id"]

    discovered_id = conn.execute(
        """
        INSERT INTO discovered_documents (source_id, url, discovered_at, last_seen_at)
        VALUES (?, ?, ?, ?)
        """,
        (source_id, f"https://tn.gov.in/doc/{unique}", now, now),
    ).lastrowid
    document_id = conn.execute(
        """
        INSERT INTO documents
            (discovered_id, source_id, source_url, file_name, stored_path, sha256, byte_size, downloaded_at)
        VALUES (?, ?, ?, 'go.pdf', 'go.pdf', ?, 100, ?)
        """,
        (discovered_id, source_id, f"https://tn.gov.in/doc/{unique}", f"sha-{unique}", now),
    ).lastrowid
    extraction_id = conn.execute(
        """
        INSERT INTO extractions (document_id, backend, page_count, char_count, confidence, extracted_at)
        VALUES (?, 'pymupdf', 1, 100, 0.9, ?)
        """,
        (document_id, now),
    ).lastrowid
    record_id = conn.execute(
        """
        INSERT INTO go_records (extraction_id, document_id, source_id, extractor_version, created_at)
        VALUES (?, ?, ?, 'test-1.0', ?)
        """,
        (extraction_id, document_id, source_id, now),
    ).lastrowid

    if go_number is not None:
        conn.execute(
            """
            INSERT INTO go_fields
                (record_id, field_name, value, normalized_value, source_page, source_text, confidence, method, created_at)
            VALUES (?, 'go_number', ?, ?, 1, 'evidence', 0.9, 'test', ?)
            """,
            (record_id, go_number, go_number, now),
        )
    if go_date is not None:
        conn.execute(
            """
            INSERT INTO go_fields
                (record_id, field_name, value, normalized_value, source_page, source_text, confidence, method, created_at)
            VALUES (?, 'go_date', ?, ?, 1, 'evidence', 0.9, 'test', ?)
            """,
            (record_id, go_date, go_date, now),
        )
    return int(record_id)


def test_compute_identity_uses_the_source_department_abbreviation(conn):
    record_id = _insert_record(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-06-01"
    )
    go_identity.compute_identity(conn, record_id)

    row = conn.execute("SELECT * FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert row["go_number_numeric"] == 41
    assert row["go_year"] == 2022
    assert row["go_identifier"] == "GO41/2022"
    assert row["canonical_go_id"] == "HFW-MS-GO41/2022"


def test_compute_identity_falls_back_to_a_generic_prefix_for_an_unmapped_department(conn):
    record_id = _insert_record(
        conn, department="Some Uncatalogued Department", go_number="G.O. No.9", go_date="2021-01-01"
    )
    go_identity.compute_identity(conn, record_id)

    row = conn.execute("SELECT canonical_go_id FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert row["canonical_go_id"] == "GO-NS-GO9/2021"


def test_compute_identity_leaves_all_columns_null_when_go_number_is_missing(conn):
    record_id = _insert_record(
        conn, department="Health and Family Welfare", go_number=None, go_date="2022-06-01"
    )
    go_identity.compute_identity(conn, record_id)

    row = conn.execute("SELECT * FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert row["go_number_numeric"] is None
    assert row["go_year"] is None
    assert row["go_identifier"] is None
    assert row["canonical_go_id"] is None


def test_compute_identity_leaves_all_columns_null_when_go_date_is_missing(conn):
    record_id = _insert_record(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date=None
    )
    go_identity.compute_identity(conn, record_id)

    row = conn.execute("SELECT go_identifier FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert row["go_identifier"] is None


def test_compute_identity_leaves_all_columns_null_for_an_unparseable_go_number(conn):
    record_id = _insert_record(
        conn, department="Health and Family Welfare", go_number="garbled text", go_date="2022-06-01"
    )
    go_identity.compute_identity(conn, record_id)

    row = conn.execute("SELECT go_identifier, go_number_raw FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert row["go_identifier"] is None
    assert row["go_number_raw"] == "garbled text"


def test_compute_identity_disambiguates_a_true_collision(conn):
    # Same department, series, number and year -- forced duplicate.
    first = _insert_record(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-06-01"
    )
    second = _insert_record(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-11-30"
    )
    go_identity.compute_identity(conn, first)
    go_identity.compute_identity(conn, second)

    row_first = conn.execute("SELECT canonical_go_id FROM go_records WHERE id = ?", (first,)).fetchone()
    row_second = conn.execute("SELECT canonical_go_id FROM go_records WHERE id = ?", (second,)).fetchone()

    assert row_first["canonical_go_id"] == "HFW-MS-GO41/2022"
    assert row_second["canonical_go_id"] == f"HFW-MS-GO41/2022-R{second}"
    assert row_first["canonical_go_id"] != row_second["canonical_go_id"]


def test_compute_identity_is_idempotent(conn):
    record_id = _insert_record(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-06-01"
    )
    go_identity.compute_identity(conn, record_id)
    go_identity.compute_identity(conn, record_id)

    row = conn.execute("SELECT canonical_go_id FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert row["canonical_go_id"] == "HFW-MS-GO41/2022"


def test_compute_identity_sets_a_url_safe_slug_derived_from_canonical_go_id(conn):
    record_id = _insert_record(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-06-01"
    )
    go_identity.compute_identity(conn, record_id)

    row = conn.execute("SELECT canonical_go_id, go_url_slug FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert row["go_url_slug"] == "HFW-MS-GO41-2022"
    assert "/" not in row["go_url_slug"]


def test_compute_identity_slug_stays_unique_through_a_collision(conn):
    first = _insert_record(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-06-01"
    )
    second = _insert_record(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-11-30"
    )
    go_identity.compute_identity(conn, first)
    go_identity.compute_identity(conn, second)

    slug_first = conn.execute("SELECT go_url_slug FROM go_records WHERE id = ?", (first,)).fetchone()["go_url_slug"]
    slug_second = conn.execute("SELECT go_url_slug FROM go_records WHERE id = ?", (second,)).fetchone()["go_url_slug"]
    assert slug_first != slug_second
    assert slug_second == f"HFW-MS-GO41-2022-R{second}"


def test_new_departments_have_abbreviations_with_no_collisions(conn):
    codes = list(go_identity.DEPARTMENT_ABBREVIATIONS.values())
    assert len(codes) == len(set(codes)), "duplicate department abbreviation codes"
    assert go_identity.DEPARTMENT_ABBREVIATIONS["Law"] == "LAW"
    assert go_identity.DEPARTMENT_ABBREVIATIONS["School Education"] == "EDU"


# ---------------------------------------------------------------------------
# migrate_department_code
# ---------------------------------------------------------------------------
def test_migrate_department_code_recomputes_every_record_under_that_department(conn):
    record_id = _insert_record(
        conn, department="School Education", go_number="G.O.(Ms) No.7", go_date="2021-04-01"
    )
    # Simulate the pre-rename state: identity computed under the old "SE" code.
    conn.execute(
        "UPDATE go_records SET canonical_go_id = ?, go_url_slug = ? WHERE id = ?",
        ("SE-MS-GO7/2021", "SE-MS-GO7-2021", record_id),
    )

    touched = go_identity.migrate_department_code(conn, "School Education")
    assert touched == 1

    row = conn.execute("SELECT canonical_go_id, go_url_slug FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert row["canonical_go_id"] == "EDU-MS-GO7/2021"
    assert row["go_url_slug"] == "EDU-MS-GO7-2021"


def test_migrate_department_code_ignores_other_departments(conn):
    record_id = _insert_record(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-06-01"
    )
    touched = go_identity.migrate_department_code(conn, "School Education")
    assert touched == 0

    row = conn.execute("SELECT canonical_go_id FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert row["canonical_go_id"] is None  # never touched, since compute_identity was never called


# ---------------------------------------------------------------------------
# backfill_all
# ---------------------------------------------------------------------------
def test_backfill_all_populates_every_record_missing_an_identifier(conn):
    first = _insert_record(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-06-01"
    )
    second = _insert_record(
        conn, department="School Education", go_number="G.O.(Rt) No.7", go_date="2021-04-01"
    )

    touched = go_identity.backfill_all(conn)
    assert touched == 2

    assert conn.execute(
        "SELECT go_identifier FROM go_records WHERE id = ?", (first,)
    ).fetchone()["go_identifier"] == "GO41/2022"
    assert conn.execute(
        "SELECT go_identifier FROM go_records WHERE id = ?", (second,)
    ).fetchone()["go_identifier"] == "GO7/2021"


def test_backfill_all_fills_in_a_slug_missing_from_an_older_computation(conn):
    """Regression: a record computed before go_url_slug existed already has
    a non-NULL go_identifier, so a naive `WHERE go_identifier IS NULL` check
    would never revisit it and its slug would stay permanently missing."""
    record_id = _insert_record(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-06-01"
    )
    conn.execute(
        "UPDATE go_records SET go_identifier = ?, canonical_go_id = ?, go_url_slug = NULL WHERE id = ?",
        ("GO41/2022", "HFW-MS-GO41/2022", record_id),
    )

    touched = go_identity.backfill_all(conn)
    assert touched == 1

    row = conn.execute("SELECT go_url_slug FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert row["go_url_slug"] == "HFW-MS-GO41-2022"


def test_backfill_all_is_a_no_op_the_second_time(conn):
    _insert_record(conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2022-06-01")

    assert go_identity.backfill_all(conn) == 1
    assert go_identity.backfill_all(conn) == 0


# ---------------------------------------------------------------------------
# Wiring: extract_and_store and correct_field both keep identity current.
# ---------------------------------------------------------------------------
@pytest.fixture
def records(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    return [int(r["id"]) for r in conn.execute("SELECT id FROM go_records ORDER BY id").fetchall()]


def test_extract_and_store_computes_identity_for_a_new_record(conn, records):
    row = conn.execute(
        "SELECT go_identifier, canonical_go_id FROM go_records WHERE id = ?", (records[0],)
    ).fetchone()
    # sample GO #1 is "G.O.(Ms) No.123" dated 2026-03-15 (see sampledata.py)
    assert row["go_identifier"] == "GO123/2026"
    assert row["canonical_go_id"] is not None


def test_correcting_go_number_recomputes_identity(conn, records):
    record_id = records[0]
    review.correct_field(conn, record_id, "go_number", "G.O.(Ms) No.999", reviewer="alex")

    row = conn.execute("SELECT go_identifier FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert row["go_identifier"] == "GO999/2026"


def test_correcting_an_unrelated_field_does_not_touch_identity(conn, records):
    record_id = records[0]
    before = conn.execute("SELECT go_identifier FROM go_records WHERE id = ?", (record_id,)).fetchone()["go_identifier"]

    review.correct_field(conn, record_id, "subject", "A different subject entirely", reviewer="alex")

    after = conn.execute("SELECT go_identifier FROM go_records WHERE id = ?", (record_id,)).fetchone()["go_identifier"]
    assert after == before


# ---------------------------------------------------------------------------
# Phase 3.7 Initiative 2 -- GO Identity Validation, using the blueprint's own
# literal examples for direct traceability from "the blueprint says X" to
# "a passing test proves X".
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "go_number, go_date, expected_identifier",
    [
        ("G.O.(Ms) No.41", "2026-04-01", "GO41/2026"),
        ("G.O.(Ms) No.78", "2025-04-01", "GO78/2025"),
        ("G.O.(Ms) No.231", "2023-04-01", "GO231/2023"),
    ],
)
def test_citizen_facing_identifiers_match_the_blueprints_examples(conn, go_number, go_date, expected_identifier):
    record_id = _insert_record(conn, department="Health and Family Welfare", go_number=go_number, go_date=go_date)
    go_identity.compute_identity(conn, record_id)
    row = conn.execute("SELECT go_identifier FROM go_records WHERE id = ?", (record_id,)).fetchone()
    assert row["go_identifier"] == expected_identifier


def test_canonical_ids_match_the_blueprints_examples(conn):
    hfw_record = _insert_record(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2026-04-01"
    )
    go_identity.compute_identity(conn, hfw_record)
    hfw_row = conn.execute("SELECT canonical_go_id FROM go_records WHERE id = ?", (hfw_record,)).fetchone()
    assert hfw_row["canonical_go_id"] == "HFW-MS-GO41/2026"

    edu_record = _insert_record(
        conn, department="School Education", go_number="G.O.(Ms) No.78", go_date="2025-04-01"
    )
    go_identity.compute_identity(conn, edu_record)
    edu_row = conn.execute("SELECT canonical_go_id FROM go_records WHERE id = ?", (edu_record,)).fetchone()
    assert edu_row["canonical_go_id"] == "EDU-MS-GO78/2025"


def test_zero_collisions_among_the_blueprints_example_ids(conn):
    """Two different real GOs must never resolve to the same canonical ID,
    even when their department/series/number/year all genuinely match --
    the collision-disambiguation guarantee, confirmed by name against the
    blueprint's own example numbers rather than arbitrary ones."""
    first = _insert_record(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2026-04-01"
    )
    second = _insert_record(
        conn, department="Health and Family Welfare", go_number="G.O.(Ms) No.41", go_date="2026-11-30"
    )
    go_identity.compute_identity(conn, first)
    go_identity.compute_identity(conn, second)

    canonical_ids = {
        row["id"]: row["canonical_go_id"]
        for row in conn.execute(
            "SELECT id, canonical_go_id FROM go_records WHERE id IN (?, ?)", (first, second)
        ).fetchall()
    }
    assert canonical_ids[first] == "HFW-MS-GO41/2026"
    assert canonical_ids[second] == f"HFW-MS-GO41/2026-R{second}"
    assert len(set(canonical_ids.values())) == 2  # zero collisions
