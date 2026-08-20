"""Modules 1 & 2 -- State and District Management.

Both follow the same shape as Phase 1's source registry: a master table an
administrator edits through the UI, with every write funneled through the
shared audit_log. District certification/publication status is computed
from the sources that apply to it -- see the module docstring in
schema_phase3.sql for why that coverage is necessarily coarse (per-document
geography tagging is Phase 4's job).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .. import audit
from ..db import utcnow

# ---------------------------------------------------------------------------
# Module 1: States
# ---------------------------------------------------------------------------
STATE_STATUSES = ("NEW", "CONFIGURED", "ACTIVE", "RETIRED")
_STATE_FORWARD_ORDER = {status: index for index, status in enumerate(STATE_STATUSES)}


class GeographyError(ValueError):
    pass


@dataclass(frozen=True)
class State:
    id: int
    name: str
    code: str
    status: str
    active: bool
    launch_date: str | None


def add_state(
    conn: sqlite3.Connection, *, name: str, code: str, launch_date: str | None = None,
    actor: str,
) -> int:
    if not name.strip() or not code.strip():
        raise GeographyError("state name and code are required")
    existing = conn.execute(
        "SELECT id FROM states WHERE name = ? OR code = ?", (name, code.upper())
    ).fetchone()
    if existing is not None:
        raise GeographyError(f"a state named {name!r} or coded {code!r} already exists")

    cur = conn.execute(
        "INSERT INTO states (name, code, launch_date, created_at) VALUES (?, ?, ?, ?)",
        (name.strip(), code.strip().upper(), launch_date, utcnow()),
    )
    state_id = int(cur.lastrowid)
    audit.record(
        conn, action="state.added", entity_type="state", entity_id=state_id, actor=actor,
        detail={"name": name, "code": code},
    )
    return state_id


def set_state_status(conn: sqlite3.Connection, state_id: int, status: str, *, actor: str) -> None:
    if status not in STATE_STATUSES:
        raise GeographyError(f"status must be one of {STATE_STATUSES}")
    row = conn.execute("SELECT status FROM states WHERE id = ?", (state_id,)).fetchone()
    if row is None:
        raise LookupError(f"no state with id {state_id}")
    before = row["status"]

    # Forward-only along the lifecycle, except RETIRED, which is reachable
    # from anywhere -- a state can be decommissioned at any stage.
    if status != "RETIRED" and _STATE_FORWARD_ORDER[status] < _STATE_FORWARD_ORDER[before]:
        raise GeographyError(f"cannot move state backward from {before} to {status}")

    conn.execute("UPDATE states SET status = ? WHERE id = ?", (status, state_id))
    audit.record(
        conn, action="state.status_changed", entity_type="state", entity_id=state_id, actor=actor,
        field_name="status", before_value=before, after_value=status,
    )


def set_state_active(conn: sqlite3.Connection, state_id: int, active: bool, *, actor: str) -> None:
    row = conn.execute("SELECT active FROM states WHERE id = ?", (state_id,)).fetchone()
    if row is None:
        raise LookupError(f"no state with id {state_id}")
    conn.execute("UPDATE states SET active = ? WHERE id = ?", (1 if active else 0, state_id))
    audit.record(
        conn, action="state.activated" if active else "state.deactivated",
        entity_type="state", entity_id=state_id, actor=actor,
        field_name="active", before_value=bool(row["active"]), after_value=active,
    )


def get_state(conn: sqlite3.Connection, state_id: int) -> State | None:
    row = conn.execute("SELECT * FROM states WHERE id = ?", (state_id,)).fetchone()
    return _row_to_state(row) if row else None


def list_states(conn: sqlite3.Connection) -> list[State]:
    return [_row_to_state(r) for r in conn.execute("SELECT * FROM states ORDER BY name").fetchall()]


def _row_to_state(row: sqlite3.Row) -> State:
    return State(
        id=int(row["id"]), name=row["name"], code=row["code"], status=row["status"],
        active=bool(row["active"]), launch_date=row["launch_date"],
    )


# ---------------------------------------------------------------------------
# Module 2: Districts
# ---------------------------------------------------------------------------
DISTRICT_STATUSES = ("NEW", "CONFIGURED", "CERTIFYING", "CERTIFIED", "PUBLISHED")
_DISTRICT_FORWARD_ORDER = {status: index for index, status in enumerate(DISTRICT_STATUSES)}


@dataclass(frozen=True)
class District:
    id: int
    state_id: int
    name: str
    code: str
    status: str
    certification_status: str
    publication_status: str


def add_district(
    conn: sqlite3.Connection, *, state_id: int, name: str, code: str, actor: str
) -> int:
    if get_state(conn, state_id) is None:
        raise LookupError(f"no state with id {state_id}")
    if not name.strip() or not code.strip():
        raise GeographyError("district name and code are required")

    existing = conn.execute(
        "SELECT id FROM districts WHERE state_id = ? AND code = ?", (state_id, code.upper())
    ).fetchone()
    if existing is not None:
        raise GeographyError(f"district code {code!r} already exists in this state")

    cur = conn.execute(
        "INSERT INTO districts (state_id, name, code, created_at) VALUES (?, ?, ?, ?)",
        (state_id, name.strip(), code.strip().upper(), utcnow()),
    )
    district_id = int(cur.lastrowid)
    audit.record(
        conn, action="district.added", entity_type="district", entity_id=district_id, actor=actor,
        detail={"state_id": state_id, "name": name, "code": code},
    )
    return district_id


def set_district_status(
    conn: sqlite3.Connection, district_id: int, status: str, *, actor: str
) -> None:
    if status not in DISTRICT_STATUSES:
        raise GeographyError(f"status must be one of {DISTRICT_STATUSES}")
    row = conn.execute("SELECT status FROM districts WHERE id = ?", (district_id,)).fetchone()
    if row is None:
        raise LookupError(f"no district with id {district_id}")
    before = row["status"]
    if _DISTRICT_FORWARD_ORDER[status] < _DISTRICT_FORWARD_ORDER[before]:
        raise GeographyError(f"cannot move district backward from {before} to {status}")

    conn.execute("UPDATE districts SET status = ? WHERE id = ?", (status, district_id))
    audit.record(
        conn, action="district.status_changed", entity_type="district", entity_id=district_id,
        actor=actor, field_name="status", before_value=before, after_value=status,
    )


def get_district(conn: sqlite3.Connection, district_id: int) -> District | None:
    row = conn.execute("SELECT * FROM districts WHERE id = ?", (district_id,)).fetchone()
    return _row_to_district(row) if row else None


def list_districts(conn: sqlite3.Connection, *, state_id: int | None = None) -> list[District]:
    sql = "SELECT * FROM districts"
    params: tuple = ()
    if state_id is not None:
        sql += " WHERE state_id = ?"
        params = (state_id,)
    sql += " ORDER BY name"
    return [_row_to_district(r) for r in conn.execute(sql, params).fetchall()]


def _row_to_district(row: sqlite3.Row) -> District:
    return District(
        id=int(row["id"]), state_id=int(row["state_id"]), name=row["name"], code=row["code"],
        status=row["status"], certification_status=row["certification_status"],
        publication_status=row["publication_status"],
    )


# ---------------------------------------------------------------------------
# District certification coverage
# ---------------------------------------------------------------------------
def applicable_sources(conn: sqlite3.Connection, district_id: int) -> list[sqlite3.Row]:
    """Sources that count toward this district: explicitly scoped to it, or
    state-wide (district_id NULL) under the same state."""
    district = get_district(conn, district_id)
    if district is None:
        raise LookupError(f"no district with id {district_id}")
    return conn.execute(
        """
        SELECT * FROM sources
         WHERE active = 1 AND (district_id = ? OR (district_id IS NULL AND state_id = ?))
        """,
        (district_id, district.state_id),
    ).fetchall()


def districts_affected_by_source(conn: sqlite3.Connection, source_id: int) -> list[int]:
    """Districts whose certification_status depends on this source -- the
    reverse of applicable_sources(): a district-scoped source affects just
    its own district; a state-wide source (district_id NULL) affects every
    district in that state. Lets a source-certification action refresh
    exactly the districts it could have changed, instead of requiring a
    separate manual "Refresh Certification" click per district that was
    easy to forget existed."""
    row = conn.execute("SELECT district_id, state_id FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        return []
    if row["district_id"] is not None:
        return [int(row["district_id"])]
    return [
        int(r["id"]) for r in conn.execute(
            "SELECT id FROM districts WHERE state_id = ?", (row["state_id"],)
        ).fetchall()
    ]


def refresh_district_certification(conn: sqlite3.Connection, district_id: int, *, actor: str) -> str:
    """Recompute a district's certification_status from its applicable
    sources' own Phase 2 certification results. Call after any certification
    run that could have touched a source covering this district."""
    sources = applicable_sources(conn, district_id)
    if not sources:
        new_status = "PENDING"
    else:
        statuses = {s["certification_status"] for s in sources}
        if statuses == {"CERTIFIED"}:
            new_status = "CERTIFIED"
        elif "CERTIFIED" in statuses or "PARTIALLY_CERTIFIED" in statuses:
            new_status = "PARTIALLY_CERTIFIED"
        elif statuses == {"FAILED"}:
            new_status = "FAILED"
        else:
            new_status = "PENDING"

    row = conn.execute(
        "SELECT certification_status FROM districts WHERE id = ?", (district_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no district with id {district_id}")
    before = row["certification_status"]

    conn.execute(
        "UPDATE districts SET certification_status = ? WHERE id = ?", (new_status, district_id)
    )
    if new_status == "CERTIFIED" and before != "CERTIFIED":
        # Certification achieved -- reflect it in the operational lifecycle too.
        district = get_district(conn, district_id)
        if _DISTRICT_FORWARD_ORDER.get(district.status, 0) < _DISTRICT_FORWARD_ORDER["CERTIFIED"]:
            set_district_status(conn, district_id, "CERTIFIED", actor=actor)

    if new_status != before:
        audit.record(
            conn, action="district.certification_refreshed", entity_type="district",
            entity_id=district_id, actor=actor, field_name="certification_status",
            before_value=before, after_value=new_status,
        )
    return new_status
