"""Public read-only access to the Verified GO Database.

Everything here is scoped to `go_records.status == 'approved'` -- the same
boundary `review.verified_records()` already enforces. A pending or rejected
record's existence must never be distinguishable from a nonexistent one to a
public caller, so `get()` and `document_id_for()` both return `None` for
either case rather than raising a lookup error that could leak which.

Only evidence a citizen should see crosses this module's boundary: the
extracted value, the page it came from, and the quoted source text. Reviewer
machinery (confidence scores, extraction method names, audit/provenance
history, reviewer identity) stays in `review.py` and is never surfaced here.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from .review import STATUS_APPROVED

# Human-readable labels for certification/categorize.py's ALL_BUCKETS, used
# only for display -- the bucket_key strings themselves are the query values.
DEPARTMENT_LABELS: dict[str, str] = {
    "health": "Health",
    "education": "Education",
    "public_works": "Public Works",
    "rural_development": "Rural Development",
    "other": "Other",
}

# Matches the citizen-facing "GO41/2022" / "GO41" identifier format
# (goengine/go_identity.py) so search can filter numerically instead of by
# free-text LIKE. Anything else -- old-format queries like "G.O.(Ms) No.41"
# or a subject fragment -- falls through to the existing LIKE match.
_GO_IDENTIFIER_QUERY_RE = re.compile(
    r"^GO\s*(?P<number>\d{1,5})\s*(?:/\s*(?P<year>(?:19|20)\d{2}))?$", re.IGNORECASE
)

# Phase 3.5 Initiative 8 -- Search Excellence. "HFW GO41" / "HFW GO41/2022":
# a department code (go_identity.DEPARTMENT_ABBREVIATIONS' values) followed
# by a GO identifier. Matches on canonical_go_id's own code prefix -- no
# reverse lookup of the abbreviation table needed, and no risk of it drifting
# out of sync with go_identity.py, since canonical_go_id is computed from
# that exact table already.
_DEPARTMENT_CODE_GO_QUERY_RE = re.compile(
    r"^(?P<code>[A-Za-z]{2,6})\s+GO\s*(?P<number>\d{1,5})\s*(?:/\s*(?P<year>(?:19|20)\d{2}))?$", re.IGNORECASE
)

# A bare 4-digit year ("2022") filters on go_year directly instead of
# falling through to a LIKE, which could false-positive on an unrelated
# 4-digit run of digits inside a subject line.
_YEAR_QUERY_RE = re.compile(r"^(?:19|20)\d{2}$")


@dataclass
class PublicRecord:
    record_id: int
    document_id: int
    go_number: str | None
    go_identifier: str | None
    go_url_slug: str | None
    go_date: str | None
    department: str | None
    district: str | None
    subject: str | None
    source_url: str
    file_name: str
    sha256: str
    page_count: int
    fields: dict[str, dict] = field(default_factory=dict)


def _load_fields(conn: sqlite3.Connection, record_id: int) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT field_name, normalized_value, source_page, source_text
          FROM go_fields
         WHERE record_id = ? AND superseded_by IS NULL
         ORDER BY field_name
        """,
        (record_id,),
    ).fetchall()
    return {
        r["field_name"]: {
            "value": r["normalized_value"],
            "source_page": int(r["source_page"]),
            "source_text": r["source_text"],
        }
        for r in rows
    }


def _build_record(conn: sqlite3.Connection, row: sqlite3.Row) -> PublicRecord:
    fields = _load_fields(conn, int(row["id"]))

    def val(name: str) -> str | None:
        f = fields.get(name)
        return f["value"] if f else None

    return PublicRecord(
        record_id=int(row["id"]),
        document_id=int(row["document_id"]),
        go_number=val("go_number"),
        go_identifier=row["go_identifier"],
        go_url_slug=row["go_url_slug"],
        go_date=val("go_date"),
        department=val("department"),
        district=val("district"),
        subject=val("subject"),
        source_url=row["source_url"],
        file_name=row["file_name"],
        sha256=row["sha256"],
        page_count=int(row["page_count"]),
        fields=fields,
    )


def _filters(
    *, department_bucket: str | None, district: str | None, q: str | None
) -> tuple[str, str, list]:
    """Returns (extra_joins, where_sql, params) shared by search()'s count
    and page queries, so the two can never drift out of sync with each
    other."""
    where = ["r.status = ?"]
    params: list = [STATUS_APPROVED]
    joins = ""
    if department_bucket:
        joins += " LEFT JOIN document_categories dc ON dc.document_id = d.id"
        where.append("dc.department_bucket = ?")
        params.append(department_bucket)
    if district:
        where.append(
            "EXISTS (SELECT 1 FROM go_fields f WHERE f.record_id = r.id "
            "AND f.field_name = 'district' AND f.superseded_by IS NULL AND f.normalized_value = ?)"
        )
        params.append(district)
    if q:
        q_stripped = q.strip()
        dept_code_match = _DEPARTMENT_CODE_GO_QUERY_RE.match(q_stripped)
        go_match = _GO_IDENTIFIER_QUERY_RE.match(q_stripped)
        year_match = _YEAR_QUERY_RE.match(q_stripped)

        if dept_code_match:
            where.append("r.go_number_numeric = ? AND r.canonical_go_id LIKE ?")
            params.append(int(dept_code_match.group("number")))
            params.append(f"{dept_code_match.group('code').upper()}-%")
            if dept_code_match.group("year"):
                where.append("r.go_year = ?")
                params.append(int(dept_code_match.group("year")))
        elif go_match:
            where.append("r.go_number_numeric = ?")
            params.append(int(go_match.group("number")))
            if go_match.group("year"):
                where.append("r.go_year = ?")
                params.append(int(go_match.group("year")))
        elif year_match:
            where.append("r.go_year = ?")
            params.append(int(q_stripped))
        else:
            # Free-text fallback, OR'd with an exact (case-insensitive)
            # department name match -- "Health and Family Welfare" typed
            # verbatim finds that department's GOs even though it never
            # appears in go_number/subject text.
            where.append(
                "(EXISTS (SELECT 1 FROM go_fields f WHERE f.record_id = r.id "
                "AND f.field_name IN ('go_number', 'subject') AND f.superseded_by IS NULL "
                "AND f.normalized_value LIKE ?) "
                "OR EXISTS (SELECT 1 FROM sources src WHERE src.id = r.source_id AND LOWER(src.department) = LOWER(?)))"
            )
            params.append(f"%{q_stripped}%")
            params.append(q_stripped)
    return joins, " AND ".join(where), params


def search(
    conn: sqlite3.Connection,
    *,
    department_bucket: str | None = None,
    district: str | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[PublicRecord], int]:
    """Approved records only, newest first. Returns (records, total_count)."""
    joins, where_sql, params = _filters(department_bucket=department_bucket, district=district, q=q)

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM go_records r JOIN documents d ON d.id = r.document_id{joins} WHERE {where_sql}",
        params,
    ).fetchone()["n"]

    rows = conn.execute(
        f"""
        SELECT r.id, r.document_id, r.go_identifier, r.go_url_slug, d.source_url, d.file_name, d.sha256, e.page_count
          FROM go_records r
          JOIN documents d ON d.id = r.document_id
          JOIN extractions e ON e.id = r.extraction_id
          {joins}
         WHERE {where_sql}
         ORDER BY r.id DESC
         LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    return [_build_record(conn, row) for row in rows], int(total)


def get(conn: sqlite3.Connection, record_id: int) -> PublicRecord | None:
    row = conn.execute(
        """
        SELECT r.id, r.document_id, r.go_identifier, r.go_url_slug, d.source_url, d.file_name, d.sha256, e.page_count
          FROM go_records r
          JOIN documents d ON d.id = r.document_id
          JOIN extractions e ON e.id = r.extraction_id
         WHERE r.id = ? AND r.status = ?
        """,
        (record_id, STATUS_APPROVED),
    ).fetchone()
    if row is None:
        return None
    return _build_record(conn, row)


def get_by_slug(conn: sqlite3.Connection, slug: str) -> PublicRecord | None:
    """Resolves the permanent GO URL (/go/{slug}) to its record. Same
    approved-only gating as get() -- a pending/rejected record's slug must
    never resolve, even if a citizen happens to guess it."""
    row = conn.execute(
        """
        SELECT r.id, r.document_id, r.go_identifier, r.go_url_slug, d.source_url, d.file_name, d.sha256, e.page_count
          FROM go_records r
          JOIN documents d ON d.id = r.document_id
          JOIN extractions e ON e.id = r.extraction_id
         WHERE r.go_url_slug = ? AND r.status = ?
        """,
        (slug, STATUS_APPROVED),
    ).fetchone()
    if row is None:
        return None
    return _build_record(conn, row)


def get_full_text(conn: sqlite3.Connection, record_id: int) -> str | None:
    """The extracted page text of an approved record's document, page-break
    joined -- backs the citizen "download OCR text" format. Same approved-only
    gating as get()."""
    row = conn.execute(
        "SELECT extraction_id FROM go_records WHERE id = ? AND status = ?",
        (record_id, STATUS_APPROVED),
    ).fetchone()
    if row is None:
        return None
    pages = conn.execute(
        "SELECT text FROM extraction_pages WHERE extraction_id = ? ORDER BY page_number",
        (row["extraction_id"],),
    ).fetchall()
    return "\n\n----- page break -----\n\n".join(p["text"] for p in pages)


def document_id_for(conn: sqlite3.Connection, record_id: int) -> tuple[int, str] | None:
    """(document_id, file_name) for an approved record's PDF, or None -- the
    public PDF route gates on approval status this way, not just document
    existence, so a pending/rejected document's file can't be fetched by
    guessing its record id."""
    row = conn.execute(
        """
        SELECT d.id AS document_id, d.file_name
          FROM go_records r JOIN documents d ON d.id = r.document_id
         WHERE r.id = ? AND r.status = ?
        """,
        (record_id, STATUS_APPROVED),
    ).fetchone()
    if row is None:
        return None
    return int(row["document_id"]), row["file_name"]


def filter_options(conn: sqlite3.Connection) -> dict:
    """Only filter values that actually have >=1 approved record -- avoids
    offering a dropdown option that dead-ends in an empty page."""
    departments = conn.execute(
        """
        SELECT DISTINCT dc.department_bucket AS bucket
          FROM go_records r
          JOIN documents d ON d.id = r.document_id
          JOIN document_categories dc ON dc.document_id = d.id
         WHERE r.status = ? AND dc.department_bucket IS NOT NULL
         ORDER BY bucket
        """,
        (STATUS_APPROVED,),
    ).fetchall()
    districts = conn.execute(
        """
        SELECT DISTINCT f.normalized_value AS district
          FROM go_records r
          JOIN go_fields f ON f.record_id = r.id
         WHERE r.status = ? AND f.field_name = 'district' AND f.superseded_by IS NULL
         ORDER BY district
        """,
        (STATUS_APPROVED,),
    ).fetchall()
    years = conn.execute(
        "SELECT DISTINCT go_year FROM go_records WHERE status = ? AND go_year IS NOT NULL ORDER BY go_year DESC",
        (STATUS_APPROVED,),
    ).fetchall()
    return {
        "departments": [
            {"key": r["bucket"], "label": DEPARTMENT_LABELS.get(r["bucket"], r["bucket"].title())}
            for r in departments
        ],
        "districts": [r["district"] for r in districts],
        "years": [int(r["go_year"]) for r in years],
    }
