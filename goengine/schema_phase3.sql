-- Thirdeye GO Intelligence Engine -- Phase 3 schema (Operations Control Center).
--
-- Additive to schema.sql/schema_phase2.sql: every statement is idempotent
-- (CREATE ... IF NOT EXISTS). New columns on pre-existing tables (`sources`)
-- are added via introspection in db.py, since SQLite ALTER TABLE has no
-- IF NOT EXISTS.
--
-- Modeling note on geography: Phase 1/2's source model is fundamentally
-- STATE-wide (a GO portal serves an entire state, not one district), while
-- individual documents have no per-district tag -- that mapping is Phase 4's
-- job (Geography Intelligence), explicitly deferred by the Phase 3
-- blueprint's own "Next Phase" section. `sources.district_id` is therefore
-- OPTIONAL: a source with a district set is district-specific; a source
-- with district_id NULL is state-wide and implicitly covers every district
-- in its state for certification/publication purposes. District-level
-- publication in this phase is a coarse, honest proxy (sources certified +
-- at least one approved record exists) rather than a claim that any
-- specific GO is "about" that district -- that precision doesn't exist yet.
--
-- Governance rules enforced structurally here:
--   * Source edits are never destructive: `source_versions` is append-only,
--     written on every edit, mirroring the go_fields/golden_annotations
--     supersede pattern already used in Phases 1-2.
--   * Publication has no direct write path: `publish_district`/
--     `publish_department` (Python) check certification+approval+provenance
--     before flipping a status column, and the check runs every time --
--     there is no "already published, skip re-checking" shortcut.
--   * Every administrative action funnels through the same append-only
--     audit_log as Phases 1-2 (no new audit table).

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Module 1: State Management
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS states (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    code            TEXT    NOT NULL UNIQUE,   -- e.g. 'TN'
    status          TEXT    NOT NULL DEFAULT 'NEW',
    active          INTEGER NOT NULL DEFAULT 1,
    launch_date     TEXT,
    created_at      TEXT    NOT NULL,
    CHECK (status IN ('NEW', 'CONFIGURED', 'ACTIVE', 'RETIRED')),
    CHECK (active IN (0, 1))
);

-- ---------------------------------------------------------------------------
-- Module 2: District Management
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS districts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    state_id                INTEGER NOT NULL REFERENCES states(id),
    name                    TEXT    NOT NULL,
    code                    TEXT    NOT NULL,
    status                  TEXT    NOT NULL DEFAULT 'NEW',
    certification_status    TEXT    NOT NULL DEFAULT 'PENDING',
    publication_status      TEXT    NOT NULL DEFAULT 'NOT_PUBLISHED',
    published_by            TEXT,
    published_at            TEXT,
    created_at              TEXT    NOT NULL,
    UNIQUE (state_id, code),
    CHECK (status IN ('NEW', 'CONFIGURED', 'CERTIFYING', 'CERTIFIED', 'PUBLISHED')),
    CHECK (certification_status IN ('PENDING', 'CERTIFIED', 'PARTIALLY_CERTIFIED', 'FAILED')),
    CHECK (publication_status IN ('NOT_PUBLISHED', 'PUBLISHED'))
);

CREATE INDEX IF NOT EXISTS idx_districts_state ON districts(state_id);

-- ---------------------------------------------------------------------------
-- Module 3: Department Management (master list)
-- ---------------------------------------------------------------------------
-- Enable/disable and metrics live here. `bucket_key` links a department to
-- Phase 2's existing categorization taxonomy (categorize.py) where one
-- exists; departments without a mapped bucket (e.g. Agriculture, Transport,
-- Fisheries from the blueprint's own example list) are configurable here
-- but their acquisition/publication metrics honestly report "not yet
-- trackable" rather than a fabricated number.
CREATE TABLE IF NOT EXISTS departments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    bucket_key      TEXT,                       -- health|education|public_works|rural_development|NULL
    active          INTEGER NOT NULL DEFAULT 1,
    published_by    TEXT,
    published_at    TEXT,
    publication_status TEXT NOT NULL DEFAULT 'NOT_PUBLISHED',
    created_at      TEXT    NOT NULL,
    CHECK (active IN (0, 1)),
    CHECK (publication_status IN ('NOT_PUBLISHED', 'PUBLISHED'))
);

-- ---------------------------------------------------------------------------
-- Modules 4 & 5: Government Source Registry (versioned) + Certification
-- ---------------------------------------------------------------------------
-- Append-only snapshot of every source edit. `sources` itself always holds
-- the CURRENT values (still queried directly by Phase 1/2 code); this table
-- exists purely so no edit is ever silently lossy.
CREATE TABLE IF NOT EXISTS source_versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id           INTEGER NOT NULL REFERENCES sources(id),
    version             INTEGER NOT NULL,
    name                TEXT    NOT NULL,
    department          TEXT    NOT NULL,
    url                 TEXT    NOT NULL,
    discovery_method    TEXT,
    active              INTEGER NOT NULL,
    crawl_frequency     TEXT    NOT NULL,
    changed_by          TEXT    NOT NULL,
    changed_at          TEXT    NOT NULL,
    change_reason       TEXT,
    UNIQUE (source_id, version)
);

CREATE INDEX IF NOT EXISTS idx_source_versions_source ON source_versions(source_id);

CREATE TRIGGER IF NOT EXISTS source_versions_no_update
BEFORE UPDATE ON source_versions
BEGIN
    SELECT RAISE(ABORT, 'source_versions is append-only; a new edit creates a new version');
END;

CREATE TRIGGER IF NOT EXISTS source_versions_no_delete
BEFORE DELETE ON source_versions
BEGIN
    SELECT RAISE(ABORT, 'source_versions is append-only; version history is never removed');
END;

-- ---------------------------------------------------------------------------
-- Module 6: Certification Job Center
-- ---------------------------------------------------------------------------
-- One row per background job. Progress counters are updated in place as the
-- job runs (this is operational live state, not evidence -- unlike
-- documents/go_fields/audit_log, there is no governance reason to make this
-- table append-only).
CREATE TABLE IF NOT EXISTS certification_jobs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    state_id                INTEGER REFERENCES states(id),
    district_id             INTEGER REFERENCES districts(id),
    department_filter       TEXT,               -- JSON array of department bucket keys, or NULL for all
    status                  TEXT    NOT NULL DEFAULT 'QUEUED',
    documents_found         INTEGER NOT NULL DEFAULT 0,
    documents_downloaded    INTEGER NOT NULL DEFAULT 0,
    documents_parsed        INTEGER NOT NULL DEFAULT 0,
    documents_needs_ocr     INTEGER NOT NULL DEFAULT 0,
    documents_failed        INTEGER NOT NULL DEFAULT 0,
    sources_total           INTEGER NOT NULL DEFAULT 0,
    sources_completed       INTEGER NOT NULL DEFAULT 0,
    error                   TEXT,
    created_by              TEXT    NOT NULL,
    created_at              TEXT    NOT NULL,
    started_at              TEXT,
    finished_at             TEXT,
    CHECK (status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON certification_jobs(status);

-- ---------------------------------------------------------------------------
-- Module 7: Review Workbench -- Escalation
-- ---------------------------------------------------------------------------
-- Escalation is additive, not a replacement terminal status: a record stays
-- pending/approved/rejected in go_records, and gets an escalation flag
-- layered on top for a higher-authority reviewer's attention.
CREATE TABLE IF NOT EXISTS escalations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       INTEGER NOT NULL REFERENCES go_records(id),
    escalated_by    TEXT    NOT NULL,
    escalated_at    TEXT    NOT NULL,
    reason          TEXT    NOT NULL,
    resolved        INTEGER NOT NULL DEFAULT 0,
    resolved_by     TEXT,
    resolved_at     TEXT,
    resolution_note TEXT,
    CHECK (resolved IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_escalations_record ON escalations(record_id);
CREATE INDEX IF NOT EXISTS idx_escalations_resolved ON escalations(resolved);

-- ---------------------------------------------------------------------------
-- Module 11: User & Role Management
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,       -- scrypt, stdlib hashlib -- no new dependency
    role            TEXT    NOT NULL,
    -- Only meaningful for state_admin: scopes that user's writes to one state.
    state_id        INTEGER REFERENCES states(id),
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL,
    CHECK (role IN ('platform_admin', 'state_admin', 'reviewer', 'auditor', 'read_only')),
    CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT    PRIMARY KEY,    -- random token, also the cookie value
    user_id         INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT    NOT NULL,
    expires_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
