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
