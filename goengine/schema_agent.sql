-- Phase 3.4 -- Local Extraction Agent: API keys + sync history.
CREATE TABLE IF NOT EXISTS agent_keys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT    NOT NULL,
    key_hash        TEXT    NOT NULL UNIQUE,   -- sha256 hex of the raw token
    key_prefix      TEXT    NOT NULL,          -- first 12 chars, for display only
    created_by      TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    revoked_at      TEXT,
    last_used_at    TEXT
);

CREATE TABLE IF NOT EXISTS agent_sync_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_key_id    INTEGER NOT NULL REFERENCES agent_keys(id),
    source_id       INTEGER REFERENCES sources(id),
    source_url      TEXT,
    document_id     INTEGER REFERENCES documents(id),
    go_record_id    INTEGER REFERENCES go_records(id),
    sha256          TEXT,
    byte_size       INTEGER,
    is_new_version  INTEGER NOT NULL DEFAULT 0,
    ok              INTEGER NOT NULL,
    error           TEXT,
    synced_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_sync_log_key ON agent_sync_log(agent_key_id);
CREATE INDEX IF NOT EXISTS idx_agent_sync_log_source ON agent_sync_log(source_id);

-- A "Local" extraction request queued from the Extraction Center: the
-- server can't run it (that's the whole reason it exists), so it just
-- records the request; a local agent daemon polls, claims, executes it
-- against its own unblocked network, and reports back.
CREATE TABLE IF NOT EXISTS extraction_requests (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    state_id                INTEGER REFERENCES states(id),
    district_id             INTEGER REFERENCES districts(id),
    department_filter       TEXT,  -- JSON list of bucket names, or NULL for all
    status                  TEXT NOT NULL DEFAULT 'QUEUED',
    created_by              TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    claimed_by_agent_key_id INTEGER REFERENCES agent_keys(id),
    claimed_at              TEXT,
    started_at              TEXT,
    finished_at             TEXT,
    sources_total           INTEGER NOT NULL DEFAULT 0,
    sources_completed       INTEGER NOT NULL DEFAULT 0,
    documents_found         INTEGER NOT NULL DEFAULT 0,
    documents_downloaded    INTEGER NOT NULL DEFAULT 0,
    documents_parsed        INTEGER NOT NULL DEFAULT 0,
    documents_failed        INTEGER NOT NULL DEFAULT 0,
    error                   TEXT,
    CHECK (status IN ('QUEUED', 'CLAIMED', 'RUNNING', 'COMPLETED', 'FAILED'))
);

CREATE INDEX IF NOT EXISTS idx_extraction_requests_status ON extraction_requests(status);
