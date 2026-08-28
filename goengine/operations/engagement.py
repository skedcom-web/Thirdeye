"""Third Eye 4.1.1 -- Citizen Experience Validation Edition.

Write path: `log_event()`, called from the public citizen-facing routes in
workbench/app.py (never from /ops/* admin routes, and never while a staff
`current_user` session is active -- see the call sites there). One row per
page view or click, into `engagement_events` (schema_engagement.sql).

Read path: the rest of this module, backing the `/ops/engagement` report.
Sessions are reconstructed here at query time from the raw event log (a
30-minute inactivity gap marks a new session, the standard web-analytics
convention) rather than maintained as separate mutable state -- the write
path stays a single INSERT, and there is only ever one source of truth.

Privacy: `visitor_id` is a random, anonymous, first-party cookie value
(see workbench/deps.py's VisitorCookieMiddleware) -- never linked to a
citizen account, never shared with a third party, no personal information
collected. This data answers "how do citizens use the site," nothing else.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .. import intelligence
from ..db import utcnow

EVENT_MY_AREA_VIEW = "my_area_view"
EVENT_DISTRICTS_VIEW = "districts_view"
EVENT_ORDERS_VIEW = "orders_view"
EVENT_GO_DETAIL_VIEW = "go_detail_view"
EVENT_DOWNLOAD = "download"
EVENT_TIMELINE_VIEW = "timeline_view"
EVENT_FINANCIAL_VIEW = "financial_view"
EVENT_TIMELINE_CLICK = "timeline_click"
EVENT_CATEGORY_CLICK = "category_click"

_PAGE_EVENT_TYPES = {EVENT_MY_AREA_VIEW, EVENT_DISTRICTS_VIEW, EVENT_ORDERS_VIEW, EVENT_GO_DETAIL_VIEW}
_DASHBOARD_EVENT_TYPES = {EVENT_MY_AREA_VIEW, EVENT_DISTRICTS_VIEW}
_SESSION_GAP_MINUTES = 30


def log_event(
    conn: sqlite3.Connection,
    *,
    visitor_id: str,
    event_type: str,
    path: str,
    district: str | None = None,
    category: str | None = None,
    record_id: int | None = None,
    query_used: bool = False,
) -> None:
    """One row per citizen-facing page view or click. Never raises -- a
    logging failure must never break a page render for a real visitor."""
    try:
        conn.execute(
            """
            INSERT INTO engagement_events
                (visitor_id, event_type, path, district, category, record_id, query_used, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (visitor_id, event_type, path, district, category, record_id, int(query_used), utcnow()),
        )
    except Exception:
        pass


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def district_popularity(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """View counts by district, most-viewed first -- a district with zero
    events simply doesn't appear (same 'only show what has data' precedent
    as intelligence.py's district_ranking)."""
    rows = conn.execute(
        """
        SELECT district, COUNT(*) AS views
          FROM engagement_events
         WHERE district IS NOT NULL AND occurred_at >= ?
         GROUP BY district
         ORDER BY views DESC
        """,
        (_cutoff(days),),
    ).fetchall()
    return [{"district": r["district"], "views": r["views"]} for r in rows]


def category_popularity(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """category_click counts per intelligence.SUBJECT_KEYWORDS key,
    INCLUDING zero-count categories -- unlike
    intelligence.citizen_category_counts, 'rarely used' is exactly the
    point of this report."""
    rows = conn.execute(
        """
        SELECT category, COUNT(*) AS clicks
          FROM engagement_events
         WHERE event_type = ? AND occurred_at >= ?
         GROUP BY category
        """,
        (EVENT_CATEGORY_CLICK, _cutoff(days)),
    ).fetchall()
    counts = {r["category"]: r["clicks"] for r in rows}
    return [
        {
            "key": key,
            "label": intelligence.CATEGORY_LABELS[key],
            "icon": intelligence.CATEGORY_ICONS[key],
            "clicks": counts.get(key, 0),
        }
        for key in intelligence.SUBJECT_KEYWORDS
    ]


def timeline_vs_search(conn: sqlite3.Connection, days: int = 30) -> dict:
    cutoff = _cutoff(days)
    timeline_clicks = conn.execute(
        "SELECT COUNT(*) AS n FROM engagement_events WHERE event_type = ? AND occurred_at >= ?",
        (EVENT_TIMELINE_CLICK, cutoff),
    ).fetchone()["n"]
    searches = conn.execute(
        "SELECT COUNT(*) AS n FROM engagement_events WHERE event_type = ? AND query_used = 1 AND occurred_at >= ?",
        (EVENT_ORDERS_VIEW, cutoff),
    ).fetchone()["n"]
    return {"timeline_clicks": timeline_clicks, "searches": searches}


def feature_usage(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Every event_type's total count, most-used first -- the same list's
    tail answers 'which features are rarely used' directly."""
    rows = conn.execute(
        """
        SELECT event_type, COUNT(*) AS n
          FROM engagement_events
         WHERE occurred_at >= ?
         GROUP BY event_type
         ORDER BY n DESC
        """,
        (_cutoff(days),),
    ).fetchall()
    return [{"event_type": r["event_type"], "count": r["n"]} for r in rows]


def total_downloads(conn: sqlite3.Connection, days: int = 30) -> int:
    """From download_log (Phase 4A) -- no duplicate tracking needed."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM download_log WHERE downloaded_at >= ?", (_cutoff(days),)
    ).fetchone()["n"]


def top_viewed_records(conn: sqlite3.Connection, days: int = 30, limit: int = 10) -> list[dict]:
    """Real go_detail_view counts per record, joined to that record's
    actual go_number/department/subject. Inner join on go_records means a
    since-deleted record just drops out rather than erroring. No inferred
    popularity score -- sorted by the literal view count."""
    rows = conn.execute(
        """
        SELECT e.record_id AS record_id, COUNT(*) AS views,
               gn.normalized_value AS go_number, dep.normalized_value AS department,
               subj.normalized_value AS subject
          FROM engagement_events e
          JOIN go_records r ON r.id = e.record_id
          LEFT JOIN go_fields gn ON gn.record_id = r.id AND gn.field_name = 'go_number' AND gn.superseded_by IS NULL
          LEFT JOIN go_fields dep ON dep.record_id = r.id AND dep.field_name = 'department' AND dep.superseded_by IS NULL
          LEFT JOIN go_fields subj ON subj.record_id = r.id AND subj.field_name = 'subject' AND subj.superseded_by IS NULL
         WHERE e.event_type = ? AND e.occurred_at >= ?
         GROUP BY e.record_id
         ORDER BY views DESC
         LIMIT ?
        """,
        (EVENT_GO_DETAIL_VIEW, _cutoff(days), limit),
    ).fetchall()
    return [
        {
            "record_id": r["record_id"],
            "views": r["views"],
            "go_number": r["go_number"],
            "department": r["department"],
            "subject": r["subject"],
        }
        for r in rows
    ]


def _reconstruct_sessions(conn: sqlite3.Connection, days: int) -> list[dict]:
    """Groups each visitor's events (within the report window) into
    sessions using the standard 30-minute-inactivity-gap convention, in
    Python rather than nested SQL window functions -- simpler to get right
    and to test. Each session also carries whether that visitor has any
    event before the session even outside the window (for
    return-visitor-rate, which must see history beyond just `days`)."""
    rows = conn.execute(
        "SELECT visitor_id, event_type, occurred_at FROM engagement_events "
        "WHERE occurred_at >= ? ORDER BY visitor_id, occurred_at",
        (_cutoff(days),),
    ).fetchall()

    sessions: list[dict] = []
    current: dict | None = None
    prev_visitor = None
    prev_time: datetime | None = None
    gap = timedelta(minutes=_SESSION_GAP_MINUTES)
    for row in rows:
        visitor_id = row["visitor_id"]
        occurred_at = datetime.fromisoformat(row["occurred_at"])
        starts_new_session = (
            visitor_id != prev_visitor or prev_time is None or (occurred_at - prev_time) > gap
        )
        if starts_new_session:
            if current is not None:
                sessions.append(current)
            current = {"visitor_id": visitor_id, "start": occurred_at, "end": occurred_at, "event_types": []}
        current["end"] = occurred_at
        current["event_types"].append(row["event_type"])
        prev_visitor, prev_time = visitor_id, occurred_at
    if current is not None:
        sessions.append(current)

    for session in sessions:
        prior = conn.execute(
            "SELECT 1 FROM engagement_events WHERE visitor_id = ? AND occurred_at < ? LIMIT 1",
            (session["visitor_id"], session["start"].isoformat(timespec="seconds")),
        ).fetchone()
        session["is_return_visit"] = prior is not None

    return sessions


def session_metrics(conn: sqlite3.Connection, days: int = 30) -> dict:
    """Total sessions, average duration/pages-per-session, return-visitor
    rate, and dashboard(My Area or Districts)-to-GO-detail conversion --
    all reconstructed from the raw event log, never stored separately."""
    sessions = _reconstruct_sessions(conn, days)
    total = len(sessions)
    if total == 0:
        return {
            "total_sessions": 0,
            "avg_duration_seconds": 0.0,
            "avg_pages_per_session": 0.0,
            "return_visitor_rate": 0.0,
            "dashboard_to_go_conversion": 0.0,
        }

    durations = [(s["end"] - s["start"]).total_seconds() for s in sessions]
    page_counts = [sum(1 for e in s["event_types"] if e in _PAGE_EVENT_TYPES) for s in sessions]
    return_count = sum(1 for s in sessions if s["is_return_visit"])

    dashboard_sessions = [s for s in sessions if any(e in _DASHBOARD_EVENT_TYPES for e in s["event_types"])]
    converted = sum(1 for s in dashboard_sessions if EVENT_GO_DETAIL_VIEW in s["event_types"])

    return {
        "total_sessions": total,
        "avg_duration_seconds": sum(durations) / total,
        "avg_pages_per_session": sum(page_counts) / total,
        "return_visitor_rate": return_count / total,
        "dashboard_to_go_conversion": (converted / len(dashboard_sessions)) if dashboard_sessions else 0.0,
    }


def average_timeline_depth(conn: sqlite3.Connection, days: int = 30) -> float:
    """Average number of timeline items actually clicked into, per session
    that viewed My Area -- real click-throughs, not scroll-depth (which
    would need client-side JS instrumentation this plan deliberately
    avoids). Labeled precisely as this, not as 'depth', wherever shown."""
    sessions = _reconstruct_sessions(conn, days)
    my_area_sessions = [s for s in sessions if EVENT_MY_AREA_VIEW in s["event_types"]]
    if not my_area_sessions:
        return 0.0
    clicks = [sum(1 for e in s["event_types"] if e == EVENT_TIMELINE_CLICK) for s in my_area_sessions]
    return sum(clicks) / len(my_area_sessions)


def purge_old_events(conn: sqlite3.Connection, *, months: int = 12) -> int:
    """Removes engagement_events rows older than `months`. Touches only
    this one table -- GO records, audit_log, publication history, and
    citizen accounts are never in its blast radius."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=months * 30)).isoformat(timespec="seconds")
    return conn.execute("DELETE FROM engagement_events WHERE occurred_at < ?", (cutoff,)).rowcount
