-- Phase 4.1 Refinement 9's Initiative 9 -- Backlog Reduction Campaign
-- Manager. One row per campaign an admin starts from the Review Operations
-- Dashboard; "active" means the most recently started row with no
-- ended_at. Reduction % and days-remaining are always computed live from
-- go_records/review.queue_counts against starting_count -- never stored,
-- so they can't drift from the real backlog.
CREATE TABLE IF NOT EXISTS backlog_campaigns (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT    NOT NULL,
    started_by     TEXT    NOT NULL,
    starting_count INTEGER NOT NULL,
    target_count   INTEGER NOT NULL,
    ended_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_backlog_campaigns_active ON backlog_campaigns(ended_at);
