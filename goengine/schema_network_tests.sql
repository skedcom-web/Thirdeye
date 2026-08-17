-- Network Connectivity Diagnostics table definition
CREATE TABLE IF NOT EXISTS network_connectivity_tests (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    target_name        TEXT NOT NULL,
    url                TEXT NOT NULL,
    timestamp          TEXT NOT NULL,
    status             TEXT NOT NULL,
    status_code        INTEGER,
    response_time_ms   REAL,
    duration_ms        REAL,
    response_size      INTEGER,
    content_type       TEXT,
    redirect_count     INTEGER,
    ssl_verified       INTEGER,
    user_agent         TEXT,
    failure_category   TEXT,
    failure_subtype    TEXT,
    error_message      TEXT,
    response_headers   TEXT,  -- JSON string
    response_html      TEXT   -- Snippet of HTML response
);

-- Tracks one execution of "run all connectivity diagnostics" as a background
-- job, so the UI can show live progress instead of blocking the request for
-- however long it takes to probe every control target + registered source.
CREATE TABLE IF NOT EXISTS network_diagnostic_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    status             TEXT NOT NULL DEFAULT 'QUEUED',
    targets_total      INTEGER NOT NULL DEFAULT 0,
    targets_completed  INTEGER NOT NULL DEFAULT 0,
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    started_at         TEXT,
    finished_at        TEXT,
    error              TEXT
);
