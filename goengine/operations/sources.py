"""Modules 4 & 5 -- Versioned Government Source Registry + Certification Center.

Wraps Phase 1's registry.py (host allowlist enforcement, the source row
itself) rather than duplicating it, and adds what Phase 3 needs on top:
geography linkage, a discovery-method label, an operational lifecycle
distinct from Phase 2's certification RESULT, and full version history.

"Never overwrite" is structural: `source_versions` has no UPDATE/DELETE
path in the schema (see schema_phase3.sql's triggers) -- every edit inserts
a new row, and `sources` always reflects the CURRENT version for the rest
of the app to keep querying directly.

Module 5 (the Source Certification Center) does not appear as new code
here: it is Phase 2's already-built, already-tested `certify_source`, run
from a Phase 3 admin page. Duplicating that engine would just be two
implementations of the same five checks to keep in sync.
"""

from __future__ import annotations

import sqlite3

from .. import audit, registry
from ..db import utcnow
from ..fetching import FetchError, Fetcher

DISCOVERY_METHODS = ("sitemap", "listing_page", "search_page", "direct_pdf_links")
LIFECYCLE_STATUSES = ("NEW", "TESTED", "CERTIFIED", "ACTIVE", "RETIRED")
_LIFECYCLE_ORDER = {status: index for index, status in enumerate(LIFECYCLE_STATUSES)}


class SourceOperationsError(ValueError):
    pass


def _write_version(conn: sqlite3.Connection, source_id: int, *, actor: str, reason: str | None) -> int:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    version = int(row["current_version"])
    conn.execute(
        """
        INSERT INTO source_versions
            (source_id, version, name, department, url, discovery_method, active,
             crawl_frequency, changed_by, changed_at, change_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id, version, row["name"], row["department"], row["url"], row["discovery_method"],
            row["active"], row["crawl_frequency"], actor, utcnow(), reason,
        ),
    )
    return version


def create_source(
    conn: sqlite3.Connection,
    *,
    name: str,
    department: str,
    url: str,
    source_type: str,
    discovery_method: str | None = None,
    state_id: int | None = None,
    district_id: int | None = None,
    crawl_frequency: str = "daily",
    actor: str,
) -> int:
    if discovery_method is not None and discovery_method not in DISCOVERY_METHODS:
        raise SourceOperationsError(f"discovery_method must be one of {DISCOVERY_METHODS}")

    source_id = registry.add_source(
        conn, name=name, department=department, url=url, source_type=source_type,
        crawl_frequency=crawl_frequency, actor=actor,
    )
    conn.execute(
        "UPDATE sources SET state_id = ?, district_id = ?, discovery_method = ? WHERE id = ?",
        (state_id, district_id, discovery_method, source_id),
    )
    _write_version(conn, source_id, actor=actor, reason="created")
    return source_id


def edit_source(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    name: str | None = None,
    department: str | None = None,
    url: str | None = None,
    discovery_method: str | None = None,
    crawl_frequency: str | None = None,
    actor: str,
    reason: str,
) -> int:
    """Apply an edit and record a new version. Returns the new version number."""
    if not reason:
        raise SourceOperationsError("a reason is required for every source edit")
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise LookupError(f"no source with id {source_id}")

    new_url = url or row["url"]
    if url and url != row["url"]:
        try:
            registry.assert_approved(new_url)
        except registry.SourceRejected as exc:
            raise SourceOperationsError(str(exc)) from exc

    if discovery_method is not None and discovery_method not in DISCOVERY_METHODS:
        raise SourceOperationsError(f"discovery_method must be one of {DISCOVERY_METHODS}")

    before = dict(row)
    new_version = int(row["current_version"]) + 1
    conn.execute(
        """
        UPDATE sources
           SET name = ?, department = ?, url = ?, host = ?, discovery_method = ?,
               crawl_frequency = ?, current_version = ?
         WHERE id = ?
        """,
        (
            name or row["name"], department or row["department"], new_url,
            registry.host_of(new_url), discovery_method or row["discovery_method"],
            crawl_frequency or row["crawl_frequency"], new_version, source_id,
        ),
    )
    _write_version(conn, source_id, actor=actor, reason=reason)

    audit.record(
        conn, action="source.edited", entity_type="source", entity_id=source_id, actor=actor,
        detail={
            "version": new_version, "reason": reason,
            "before": {"name": before["name"], "url": before["url"], "department": before["department"]},
        },
    )
    return new_version


def retire_source(conn: sqlite3.Connection, source_id: int, *, actor: str, reason: str) -> None:
    row = conn.execute("SELECT lifecycle_status FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise LookupError(f"no source with id {source_id}")
    before = row["lifecycle_status"]
    conn.execute(
        "UPDATE sources SET lifecycle_status = 'RETIRED', active = 0 WHERE id = ?", (source_id,)
    )
    audit.record(
        conn, action="source.retired", entity_type="source", entity_id=source_id, actor=actor,
        field_name="lifecycle_status", before_value=before, after_value="RETIRED",
        detail={"reason": reason},
    )


def clone_source(conn: sqlite3.Connection, source_id: int, *, actor: str) -> int:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise LookupError(f"no source with id {source_id}")

    clone_name = f"{row['name']} (clone)"
    suffix = 2
    while conn.execute("SELECT 1 FROM sources WHERE name = ?", (clone_name,)).fetchone():
        clone_name = f"{row['name']} (clone {suffix})"
        suffix += 1

    new_id = create_source(
        conn, name=clone_name, department=row["department"], url=row["url"],
        source_type=row["source_type"], discovery_method=row["discovery_method"],
        state_id=row["state_id"], district_id=row["district_id"],
        crawl_frequency=row["crawl_frequency"], actor=actor,
    )
    audit.record(
        conn, action="source.cloned", entity_type="source", entity_id=new_id, actor=actor,
        detail={"cloned_from": source_id},
    )
    return new_id


def quick_test_source(
    conn: sqlite3.Connection, fetcher: Fetcher, source_id: int, *, actor: str
) -> tuple[bool, str]:
    """Module 4's lightweight "Test Source" -- connectivity only. The full
    5-check battery is Module 5 (Phase 2's certify_source)."""
    row = conn.execute("SELECT url, lifecycle_status FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise LookupError(f"no source with id {source_id}")

    try:
        registry.assert_approved(row["url"])
        response = fetcher.get(row["url"])
        ok = response.status_code == 200
        message = f"HTTP {response.status_code}" if ok else f"unexpected status {response.status_code}"
    except (FetchError, registry.SourceRejected) as exc:
        ok, message = False, str(exc)

    if ok and _LIFECYCLE_ORDER.get(row["lifecycle_status"], 0) < _LIFECYCLE_ORDER["TESTED"]:
        conn.execute("UPDATE sources SET lifecycle_status = 'TESTED' WHERE id = ?", (source_id,))

    audit.record(
        conn, action="source.tested", entity_type="source", entity_id=source_id, actor=actor,
        detail={"ok": ok, "message": message},
    )
    return ok, message


def advance_lifecycle_on_certification(conn: sqlite3.Connection, source_id: int, result: str) -> None:
    """Called after a Module 5 certification run: a CERTIFIED result moves
    the operational lifecycle forward too, not just the certification_status
    result column Phase 2 already tracks."""
    if result != "CERTIFIED":
        return
    row = conn.execute("SELECT lifecycle_status FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is not None and _LIFECYCLE_ORDER.get(row["lifecycle_status"], 0) < _LIFECYCLE_ORDER["CERTIFIED"]:
        conn.execute("UPDATE sources SET lifecycle_status = 'CERTIFIED' WHERE id = ?", (source_id,))


def version_history(conn: sqlite3.Connection, source_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM source_versions WHERE source_id = ? ORDER BY version", (source_id,)
    ).fetchall()


def list_sources_with_geography(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.*, st.name AS state_name, d.name AS district_name
          FROM sources s
          LEFT JOIN states st ON st.id = s.state_id
          LEFT JOIN districts d ON d.id = s.district_id
         ORDER BY s.id
        """
    ).fetchall()
