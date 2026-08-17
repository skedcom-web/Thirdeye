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
