"""Module 1 -- Official Source Registry.

The registry is the trust boundary. A URL that is not reachable from an
active row in `sources` is never crawled and never downloaded.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from urllib.parse import urlparse

from . import audit
from .config import APPROVED_HOST_SUFFIXES, BLOCKED_HOSTS
from .db import utcnow

VALID_SOURCE_TYPES = ("go_portal", "gazette", "department_site")
VALID_FREQUENCIES = ("hourly", "daily", "weekly", "manual")


class SourceRejected(ValueError):
    """Raised when a URL fails the official-source policy."""


@dataclass(frozen=True)
class Source:
    id: int
    name: str
    department: str
    url: str
    host: str
    source_type: str
    adapter: str
    active: bool
    crawl_frequency: str
    last_crawl_at: str | None
    last_crawl_status: str | None
    notes: str | None


def host_of(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SourceRejected(f"URL must be http(s): {url!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise SourceRejected(f"URL has no host: {url!r}")
    return host


def assert_approved(url: str) -> str:
    """Validate a URL against the official-source policy. Returns its host.

    Enforced at registration time and again at download time, because a
    government page can link out to a mirror or a redirect can leave the
    approved domain.
    """
    host = host_of(url)
    if host in BLOCKED_HOSTS:
        raise SourceRejected(
            f"host {host!r} is explicitly blocked (news/aggregator, not an order source)"
        )
    for suffix in APPROVED_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return host
    raise SourceRejected(
        f"host {host!r} is not an approved government source; "
        f"approved suffixes: {', '.join(APPROVED_HOST_SUFFIXES)}"
    )


def is_approved(url: str) -> bool:
    try:
        assert_approved(url)
    except SourceRejected:
        return False
    return True


def add_source(
    conn: sqlite3.Connection,
    *,
    name: str,
    department: str,
    url: str,
    source_type: str,
    adapter: str = "generic_links",
    crawl_frequency: str = "daily",
    active: bool = True,
    notes: str | None = None,
    actor: str = audit.SYSTEM_ACTOR,
) -> int:
    if source_type not in VALID_SOURCE_TYPES:
        raise SourceRejected(f"source_type must be one of {VALID_SOURCE_TYPES}")
    if crawl_frequency not in VALID_FREQUENCIES:
        raise SourceRejected(f"crawl_frequency must be one of {VALID_FREQUENCIES}")
    host = assert_approved(url)

    cur = conn.execute(
        """
        INSERT INTO sources
            (name, department, url, host, source_type, adapter, active,
             crawl_frequency, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            department,
            url,
            host,
            source_type,
            adapter,
            1 if active else 0,
            crawl_frequency,
            notes,
            utcnow(),
        ),
    )
    source_id = int(cur.lastrowid)
    audit.record(
        conn,
        action="source.registered",
        entity_type="source",
        entity_id=source_id,
        actor=actor,
        after_value=url,
        detail={"name": name, "department": department, "host": host},
    )
    return source_id


def set_active(
    conn: sqlite3.Connection, source_id: int, active: bool, *, actor: str = audit.SYSTEM_ACTOR
) -> None:
    row = conn.execute("SELECT active FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise LookupError(f"no source with id {source_id}")
    before = bool(row["active"])
    conn.execute("UPDATE sources SET active = ? WHERE id = ?", (1 if active else 0, source_id))
    audit.record(
        conn,
        action="source.activated" if active else "source.deactivated",
        entity_type="source",
        entity_id=source_id,
        actor=actor,
        field_name="active",
        before_value=before,
        after_value=active,
    )


def mark_crawled(
    conn: sqlite3.Connection, source_id: int, status: str, *, when: str | None = None
) -> None:
    conn.execute(
        "UPDATE sources SET last_crawl_at = ?, last_crawl_status = ? WHERE id = ?",
        (when or utcnow(), status, source_id),
    )


def get_source(conn: sqlite3.Connection, source_id: int) -> Source | None:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return _row_to_source(row) if row else None


def get_by_name(conn: sqlite3.Connection, name: str) -> Source | None:
    row = conn.execute("SELECT * FROM sources WHERE name = ?", (name,)).fetchone()
    return _row_to_source(row) if row else None


def list_sources(conn: sqlite3.Connection, *, active_only: bool = False) -> list[Source]:
    sql = "SELECT * FROM sources"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY id"
    return [_row_to_source(r) for r in conn.execute(sql).fetchall()]


def _row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        id=int(row["id"]),
        name=row["name"],
        department=row["department"],
        url=row["url"],
        host=row["host"],
        source_type=row["source_type"],
        adapter=row["adapter"],
        active=bool(row["active"]),
        crawl_frequency=row["crawl_frequency"],
        last_crawl_at=row["last_crawl_at"],
        last_crawl_status=row["last_crawl_status"],
        notes=row["notes"],
    )


# ---------------------------------------------------------------------------
# Seed set from the blueprint. URLs point at Tamil Nadu government hosts and
# must be confirmed against the live portals before a production crawl --
# department landing paths on cms.tn.gov.in change between site revisions.
# ---------------------------------------------------------------------------
SEED_SOURCES: tuple[dict[str, str], ...] = (
    {
        "name": "Tamil Nadu GO Portal",
        "department": "All Departments",
        "url": "https://www.tn.gov.in/godept_list.php",
        "source_type": "go_portal",
        "adapter": "generic_links",
    },
    {
        "name": "Tamil Nadu Government Gazette",
        "department": "Stationery and Printing",
        "url": "https://stationeryprinting.tn.gov.in/extraordinary_gazette.php",
        "source_type": "gazette",
        "adapter": "generic_links",
    },
    {
        "name": "Health and Family Welfare Department",
        "department": "Health and Family Welfare",
        "url": "https://www.tn.gov.in/go.php?dep_id=MTE=&year=MjAyNg==",
        "source_type": "department_site",
        "adapter": "generic_links",
    },
    {
        "name": "School Education Department",
        "department": "School Education",
        "url": "https://www.tn.gov.in/go.php?dep_id=Mjg=&year=MjAyNg==",
        "source_type": "department_site",
        "adapter": "generic_links",
    },
    {
        "name": "Rural Development Department",
        "department": "Rural Development and Panchayat Raj",
        "url": "https://www.tn.gov.in/go.php?dep_id=Mjc=&year=MjAyNg==",
        "source_type": "department_site",
        "adapter": "generic_links",
    },
    {
        "name": "Public Works Department",
        "department": "Public Works",
        "url": "https://www.tn.gov.in/go.php?dep_id=NDI=&year=MjAyNg==",
        "source_type": "department_site",
        "adapter": "generic_links",
    },
)


def seed(conn: sqlite3.Connection, *, actor: str = audit.SYSTEM_ACTOR) -> list[int]:
    """Register the blueprint's source list. Idempotent by source name."""
    ids: list[int] = []
    for spec in SEED_SOURCES:
        existing = get_by_name(conn, spec["name"])
        if existing is not None:
            ids.append(existing.id)
            continue
        ids.append(
            add_source(
                conn,
                name=spec["name"],
                department=spec["department"],
                url=spec["url"],
                source_type=spec["source_type"],
                adapter=spec["adapter"],
                notes="Seeded from Phase 1 blueprint; confirm path against live portal.",
                actor=actor,
            )
        )
    return ids
