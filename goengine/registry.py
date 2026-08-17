"""Module 1 -- Official Source Registry.

The registry is the trust boundary. A URL that is not reachable from an
active row in `sources` is never crawled and never downloaded.
"""

from __future__ import annotations

import base64
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import audit
from .config import APPROVED_HOST_SUFFIXES, BLOCKED_HOSTS
from .db import utcnow

VALID_SOURCE_TYPES = ("go_portal", "gazette", "department_site")
VALID_FREQUENCIES = ("hourly", "daily", "weekly", "manual")

# Phase 3.3: Priority (operational triage -- which sources get attention
# first) and the blueprint's finer-grained Source Type taxonomy, kept
# separate from `source_type` above (see db.py's SOURCES_PHASE33_COLUMNS
# comment for why). Both are advisory labels, not adapter/routing logic.
VALID_PRIORITIES = ("Critical", "High", "Medium", "Low")
VALID_SOURCE_CATEGORIES = (
    "GO Portal", "Department Portal", "District Portal", "Gazette",
    "Tender Portal", "Scheme Portal", "Notification Portal",
)


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
    priority: str
    source_category: str | None


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
    priority: str = "Medium",
    source_category: str | None = None,
    actor: str = audit.SYSTEM_ACTOR,
) -> int:
    if source_type not in VALID_SOURCE_TYPES:
        raise SourceRejected(f"source_type must be one of {VALID_SOURCE_TYPES}")
    if crawl_frequency not in VALID_FREQUENCIES:
        raise SourceRejected(f"crawl_frequency must be one of {VALID_FREQUENCIES}")
    if priority not in VALID_PRIORITIES:
        raise SourceRejected(f"priority must be one of {VALID_PRIORITIES}")
    if source_category is not None and source_category not in VALID_SOURCE_CATEGORIES:
        raise SourceRejected(f"source_category must be one of {VALID_SOURCE_CATEGORIES}")
    host = assert_approved(url)

    cur = conn.execute(
        """
        INSERT INTO sources
            (name, department, url, host, source_type, adapter, active,
             crawl_frequency, notes, priority, source_category, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            priority,
            source_category,
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


def set_priority(
    conn: sqlite3.Connection, source_id: int, priority: str, *, actor: str = audit.SYSTEM_ACTOR
) -> None:
    if priority not in VALID_PRIORITIES:
        raise SourceRejected(f"priority must be one of {VALID_PRIORITIES}")
    row = conn.execute("SELECT priority FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise LookupError(f"no source with id {source_id}")
    before = row["priority"]
    conn.execute("UPDATE sources SET priority = ? WHERE id = ?", (priority, source_id))
    audit.record(
        conn, action="source.priority_changed", entity_type="source", entity_id=source_id,
        actor=actor, field_name="priority", before_value=before, after_value=priority,
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
        priority=row["priority"],
        source_category=row["source_category"],
    )


# ---------------------------------------------------------------------------
# Seed set. URLs point at Tamil Nadu government hosts.
#
# The `go.php` department listing pages take a base64-encoded `year` query
# param (e.g. `year=MjAyNg==` decodes to "2026"). Hardcoding that value goes
# stale every January, so it is templated as `{year}` here and filled in with
# the current year at seed time by `_year_param()`. Every `dep_id` below was
# read directly off the live site's own department directory
# (`godept_list.php`) on 2026-08-17, not guessed -- confirm again if a source
# stops finding documents, since the site can renumber departments.
#
# Phase 3.3's blueprint asks for its Tier 2 department list by name; two of
# them could not be verified and are deliberately left out rather than
# guessed: "Adi Dravidar Welfare" has no department of that exact name in
# the live directory (it may now be folded into Social Justice or BC/MBC
# Welfare -- unclear without asking TN government directly), and "Fisheries"
# is not a separate department on the live site -- it is combined with
# Animal Husbandry under one department, seeded as that one entry.
# ---------------------------------------------------------------------------
def _year_param() -> str:
    """Base64-encode the current year the way tn.gov.in's own links do."""
    year = str(datetime.now(timezone.utc).year)
    return base64.b64encode(year.encode()).decode()


_VERIFIED_NOTE = "dep_id verified against the live tn.gov.in department directory on 2026-08-17."

SEED_SOURCES: tuple[dict[str, str], ...] = (
    # --- Tier 1: Critical --------------------------------------------------
    {
        "name": "Tamil Nadu GO Portal",
        "department": "All Departments",
        # The department directory itself: no PDFs here, but every
        # department's go.php listing link is, which the crawler follows.
        "url": "https://www.tn.gov.in/godept_list.php",
        "source_type": "go_portal",
        "source_category": "GO Portal",
        "priority": "Critical",
        "adapter": "tn_go_portal",
    },
    {
        "name": "Tamil Nadu Government Portal",
        "department": "All Departments",
        "url": "https://www.tn.gov.in/",
        "source_type": "go_portal",
        "source_category": "GO Portal",
        "priority": "Critical",
        "adapter": "tn_go_portal",
    },
    {
        "name": "Tamil Nadu Government Gazette",
        "department": "Stationery and Printing",
        "url": "https://stationeryprinting.tn.gov.in/extraordinary_gazette.php",
        "source_type": "gazette",
        "source_category": "Gazette",
        "priority": "Critical",
        "adapter": "tn_go_portal",
    },
    # --- Tier 2: High --------------------------------------------------
    {"name": "Health and Family Welfare Department", "department": "Health and Family Welfare", "dep_id": "MTE="},
    {"name": "School Education Department", "department": "School Education", "dep_id": "Mjg="},
    {"name": "Rural Development Department", "department": "Rural Development and Panchayat Raj", "dep_id": "Mjc="},
    {"name": "Public Works Department", "department": "Public Works", "dep_id": "NDI="},
    {"name": "Agriculture Department", "department": "Agriculture and Farmers Welfare", "dep_id": "Mg=="},
    {"name": "Highways Department", "department": "Highways and Minor Ports", "dep_id": "MTM="},
    {"name": "Water Resources Department", "department": "Water Resources", "dep_id": "NDQ="},
    {"name": "Municipal Administration Department", "department": "Municipal Administration and Water Supply", "dep_id": "MjE="},
    {"name": "Revenue Department", "department": "Revenue and Disaster Management", "dep_id": "MjY="},
    {"name": "Finance Department", "department": "Finance", "dep_id": "OQ=="},
    {"name": "Housing Department", "department": "Housing and Urban Development", "dep_id": "MTU="},
    {"name": "Transport Department", "department": "Transport", "dep_id": "MzM="},
    {"name": "Industries Department", "department": "Industries, Investment Promotion and Commerce", "dep_id": "MTY="},
    {"name": "Environment Department", "department": "Environment, Climate Change and Forests", "dep_id": "OA=="},
    {"name": "Tourism Department", "department": "Tourism, Culture and Religious Endowments", "dep_id": "MzI="},
    {"name": "Labour Department", "department": "Labour Welfare and Skill Development", "dep_id": "MTg="},
    {"name": "Social Welfare Department", "department": "Social Welfare and Women Empowerment", "dep_id": "MzA="},
    {"name": "Animal Husbandry and Fisheries Department", "department": "Animal Husbandry, Dairying, Fisheries and Fishermen Welfare", "dep_id": "Mw=="},
    {"name": "Energy Department", "department": "Energy", "dep_id": "Nw=="},
    {"name": "Cooperation Department", "department": "Co-operation, Food and Consumer Protection", "dep_id": "NQ=="},
)


def seed(conn: sqlite3.Connection, *, actor: str = audit.SYSTEM_ACTOR) -> list[int]:
    """Register the source list. Idempotent by source name."""
    ids: list[int] = []
    year = _year_param()
    for spec in SEED_SOURCES:
        existing = get_by_name(conn, spec["name"])
        if existing is not None:
            ids.append(existing.id)
            continue
        if "dep_id" in spec:
            # Tier 2 department entries share the same URL/adapter/category
            # shape -- only the name, department and dep_id differ.
            url = f"https://www.tn.gov.in/go.php?dep_id={spec['dep_id']}&year={year}"
            source_type, source_category, priority = "department_site", "Department Portal", "High"
        else:
            url = spec["url"].format(year=year)
            source_type, source_category, priority = spec["source_type"], spec["source_category"], spec["priority"]
        ids.append(
            add_source(
                conn,
                name=spec["name"],
                department=spec["department"],
                url=url,
                source_type=source_type,
                source_category=source_category,
                priority=priority,
                adapter="tn_go_portal",
                notes=_VERIFIED_NOTE,
                actor=actor,
            )
        )
    return ids
