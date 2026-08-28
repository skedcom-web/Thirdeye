-- Third Eye 4.1.1 -- Citizen Experience Validation Edition.
-- One event-log row per citizen-facing page view or click. Deliberately no
-- FK on record_id (matches audit_log's existing precedent of loosely
-- referencing entities by id) -- this table is never touched by
-- operations/reset.py's production reset, since analytics is about citizen
-- behavior, not GO content.
CREATE TABLE IF NOT EXISTS engagement_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    visitor_id   TEXT    NOT NULL,
    event_type   TEXT    NOT NULL,
    path         TEXT    NOT NULL,
    district     TEXT,
    category     TEXT,
    record_id    INTEGER,
    query_used   INTEGER NOT NULL DEFAULT 0,
    occurred_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_engagement_events_visitor ON engagement_events(visitor_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_engagement_events_type ON engagement_events(event_type, occurred_at);
