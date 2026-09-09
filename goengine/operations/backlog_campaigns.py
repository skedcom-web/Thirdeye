"""Phase 4.1 Initiative 9 -- Backlog Reduction Campaign Manager.

An admin starts a campaign with a target pending count; the dashboard shows
progress against it. Reduction % and days-remaining are always computed
live from the current backlog (never stored), so they can't drift from
what review.queue_counts() actually reports.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from ..db import utcnow
from . import review as ops_review


class CampaignError(ValueError):
    pass


def start_campaign(conn: sqlite3.Connection, *, target_count: int, started_by: str) -> int:
    if target_count < 0:
        raise CampaignError("target count cannot be negative")
    if not started_by:
        raise CampaignError("a starter identity is required")
    if active_campaign(conn) is not None:
        raise CampaignError("a campaign is already active -- end it before starting another")

    starting_count = ops_review.queue_counts(conn)[ops_review.QUEUE_EXTRACTION]
    cur = conn.execute(
        """
        INSERT INTO backlog_campaigns (started_at, started_by, starting_count, target_count)
        VALUES (?, ?, ?, ?)
        """,
        (utcnow(), started_by, starting_count, target_count),
    )
    return int(cur.lastrowid)


def end_campaign(conn: sqlite3.Connection, campaign_id: int) -> None:
    conn.execute("UPDATE backlog_campaigns SET ended_at = ? WHERE id = ?", (utcnow(), campaign_id))


def active_campaign(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM backlog_campaigns WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


def campaign_progress(conn: sqlite3.Connection) -> dict | None:
    """None when no campaign is active -- the dashboard shows a "Start
    Campaign" form in that case instead of stale/fabricated numbers."""
    campaign = active_campaign(conn)
    if campaign is None:
        return None

    current = ops_review.queue_counts(conn)[ops_review.QUEUE_EXTRACTION]
    starting = int(campaign["starting_count"])
    target = int(campaign["target_count"])
    reduced = max(starting - current, 0)
    denominator = max(starting - target, 1)  # avoid div-by-zero when target >= starting
    reduction_pct = min(max(reduced / denominator * 100.0, 0.0), 100.0)

    # Days remaining: linear extrapolation from the last 7 real days' net
    # review activity -- an estimate, always labeled as one in the UI, never
    # treated as a commitment.
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    approved_week = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'record.approved' AND ts >= ?", (week_ago,)
    ).fetchone()["n"]
    rejected_week = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'record.rejected' AND ts >= ?", (week_ago,)
    ).fetchone()["n"]
    net_per_day = (approved_week + rejected_week) / 7.0
    remaining = max(current - target, 0)
    days_remaining = (remaining / net_per_day) if net_per_day > 0 else None

    return {
        "campaign_id": int(campaign["id"]),
        "started_at": campaign["started_at"],
        "started_by": campaign["started_by"],
        "starting_count": starting,
        "target_count": target,
        "current_count": current,
        "reduction_pct": reduction_pct,
        "days_remaining": days_remaining,
    }
