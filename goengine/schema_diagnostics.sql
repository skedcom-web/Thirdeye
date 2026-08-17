-- Thirdeye GO Intelligence Engine -- Phase 3.3.1 Diagnostics Schema
--

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Request-Level Crawl Evidence
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_evidences (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    crawl_run_id        INTEGER NOT NULL REFERENCES crawl_runs(id),
    url                 TEXT    NOT NULL,
    status_code         INTEGER,
    response_size       INTEGER,
    content_type        TEXT,
    response_time_ms    REAL,
    duration_ms         REAL,
    redirect_count      INTEGER DEFAULT 0,
    timestamp           TEXT    NOT NULL,
    user_agent          TEXT,
    proxy_used          TEXT,
    ssl_verified        INTEGER NOT NULL DEFAULT 1,
    error_message       TEXT,
    failure_category    TEXT, -- network_failure | discovery_failure | download_failure | parser_failure | ocr_failure | publication_failure | unknown_failure
    failure_subtype     TEXT  -- timeout | dns | ssl_error | http_403 | http_404 | http_500 | etc.
);

CREATE INDEX IF NOT EXISTS idx_crawl_evidences_run ON crawl_evidences(crawl_run_id);
CREATE INDEX IF NOT EXISTS idx_crawl_evidences_timestamp ON crawl_evidences(timestamp);

-- ---------------------------------------------------------------------------
-- Persistent Diagnostic Reports
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS diagnostic_reports (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id           INTEGER NOT NULL REFERENCES sources(id),
    run_at              TEXT    NOT NULL,
    pages_visited       INTEGER NOT NULL DEFAULT 0,
    links_found         INTEGER NOT NULL DEFAULT 0,
    dept_pages_found    INTEGER NOT NULL DEFAULT 0,
    go_listings_found   INTEGER NOT NULL DEFAULT 0,
    pdf_links_found     INTEGER NOT NULL DEFAULT 0,
    downloaded_count    INTEGER NOT NULL DEFAULT 0,
    parsed_count        INTEGER NOT NULL DEFAULT 0,
    ocr_count           INTEGER NOT NULL DEFAULT 0,
    failures_count      INTEGER NOT NULL DEFAULT 0,
    error_message       TEXT,
    avg_response_time   REAL,
    report_text         TEXT    NOT NULL,
    created_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_diagnostic_reports_source ON diagnostic_reports(source_id);

-- ---------------------------------------------------------------------------
-- System Settings
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_settings (
    key                 TEXT PRIMARY KEY,
    value               TEXT NOT NULL
);

-- Seed default retention policy setting
INSERT OR IGNORE INTO system_settings (key, value) VALUES ('diagnostics_retention_days', '30');
