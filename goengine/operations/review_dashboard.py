"""Phase 4.1 -- Review Operations Dashboard (Refinements 5 & 6, plus the
original blueprint's Initiatives 6-8). Composes triage.py's segmentation
and the existing approve/reject audit trail into one page -- no new review
criteria, no new approval path.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import review as ops_review
from . import triage

VELOCITY_WINDOW_DAYS = 30

# Refinement 5's thresholds: a starting point (5%/1% of the pending backlog
# cleared today), easy to retune once real usage data comes in -- not
# load-bearing for anything else in this module.
_THROUGHPUT_EXCELLENT = 0.05
_THROUGHPUT_GOOD = 0.01


def _today_utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def approved_today(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'record.approved' AND substr(ts, 1, 10) = ?",
        (_today_utc_date(),),
    ).fetchone()["n"]


def rejected_today(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'record.rejected' AND substr(ts, 1, 10) = ?",
        (_today_utc_date(),),
    ).fetchone()["n"]


def review_throughput_score(conn: sqlite3.Connection) -> dict:
    """Refinement 5: Approved Today / Pending Review -> Excellent / Good /
    Needs Attention. An empty backlog is trivially "Excellent" regardless
    of today's count."""
    approved = approved_today(conn)
    pending = ops_review.queue_counts(conn)[ops_review.QUEUE_EXTRACTION]
    ratio = 1.0 if pending == 0 else approved / pending
    if ratio >= _THROUGHPUT_EXCELLENT:
        label = "Excellent"
    elif ratio >= _THROUGHPUT_GOOD:
        label = "Good"
    else:
        label = "Needs Attention"
    return {"approved_today": approved, "pending_review": pending, "ratio": ratio, "label": label}


def daily_activity(conn: sqlite3.Connection, *, days: int = VELOCITY_WINDOW_DAYS) -> list[dict]:
    """One row per day with any approve/reject activity in the window --
    days with nothing recorded are simply absent, not zero-filled, so a
    long-idle stretch doesn't dominate the table."""
    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    rows = conn.execute(
        """
        SELECT substr(ts, 1, 10) AS day, action, COUNT(*) AS n
          FROM audit_log
         WHERE action IN ('record.approved', 'record.rejected') AND substr(ts, 1, 10) >= ?
         GROUP BY day, action
         ORDER BY day
        """,
        (threshold,),
    ).fetchall()
    by_day: dict[str, dict] = {}
    for r in rows:
        entry = by_day.setdefault(r["day"], {"date": r["day"], "approved": 0, "rejected": 0})
        entry["approved" if r["action"] == "record.approved" else "rejected"] = int(r["n"])
    return [by_day[d] for d in sorted(by_day, reverse=True)]


def average_review_time_hours(conn: sqlite3.Connection) -> float | None:
    row = conn.execute(
        """
        SELECT AVG((julianday(reviewed_at) - julianday(created_at)) * 24.0) AS avg_hours
          FROM go_records
         WHERE reviewed_at IS NOT NULL AND created_at IS NOT NULL
        """
    ).fetchone()
    return row["avg_hours"]


def department_leaderboard(conn: sqlite3.Connection, *, days: int = VELOCITY_WINDOW_DAYS) -> list[dict]:
    threshold = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT s.department AS department, COUNT(*) AS approvals
          FROM audit_log a
          JOIN go_records r ON r.id = a.entity_id
          JOIN sources s ON s.id = r.source_id
         WHERE a.action = 'record.approved' AND a.entity_type = 'go_record' AND a.ts >= ?
         GROUP BY s.department
         ORDER BY approvals DESC
        """,
        (threshold,),
    ).fetchall()
    return [{"department": r["department"], "approvals": int(r["approvals"])} for r in rows]


def dashboard_summary(conn: sqlite3.Connection) -> dict:
    """Everything the Review Operations Dashboard route needs, in one call."""
    return {
        "segment_counts": triage.review_segment_counts(conn),
        "approved_today": approved_today(conn),
        "rejected_today": rejected_today(conn),
        "throughput": review_throughput_score(conn),
        "top_blockers": triage.top_blockers(conn, limit=20),
        "department_triage": triage.department_blocker_counts(conn, limit=5),
        "daily_activity": daily_activity(conn),
        "average_review_time_hours": average_review_time_hours(conn),
        "department_leaderboard": department_leaderboard(conn),
    }
