-- Thirdeye GO Intelligence Engine -- Phase 1 schema.
--
-- Governance rules enforced structurally here, not by convention:
--   * A document row cannot exist without the source it came from.
--   * A GO record cannot exist without an extraction, which cannot exist
--     without an archived document with a SHA256 fingerprint.
--   * A field value cannot exist without a source page and source text.
--   * Nothing is ever UPDATEd destructively: corrections write a new field
--     row and an audit entry carrying before/after values.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Module 1: Official Source Registry
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL UNIQUE,
    department          TEXT    NOT NULL,
    url                 TEXT    NOT NULL,
    -- Host allowlist check happens at insert time in registry.py; stored so an
    -- auditor can see exactly which host was approved for this source.
    host                TEXT    NOT NULL,
    source_type         TEXT    NOT NULL,     -- go_portal | gazette | department_site
    adapter             TEXT    NOT NULL DEFAULT 'generic_links',
    active              INTEGER NOT NULL DEFAULT 1,
    crawl_frequency     TEXT    NOT NULL DEFAULT 'daily',  -- hourly|daily|weekly|manual
    last_crawl_at       TEXT,
    last_crawl_status   TEXT,
    notes               TEXT,
    created_at          TEXT    NOT NULL,
    CHECK (source_type IN ('go_portal', 'gazette', 'department_site')),
    CHECK (crawl_frequency IN ('hourly', 'daily', 'weekly', 'manual')),
    CHECK (active IN (0, 1))
);

-- ---------------------------------------------------------------------------
-- Module 2: Source Discovery Engine
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id           INTEGER NOT NULL REFERENCES sources(id),
    started_at          TEXT    NOT NULL,
    finished_at         TEXT,
    status              TEXT    NOT NULL,     -- running | ok | error
    pages_fetched       INTEGER NOT NULL DEFAULT 0,
    links_seen          INTEGER NOT NULL DEFAULT 0,
    new_documents       INTEGER NOT NULL DEFAULT 0,
    duplicate_documents INTEGER NOT NULL DEFAULT 0,
    error               TEXT
);

-- One row per distinct document URL ever seen on an approved source.
-- The UNIQUE(source_id, url) constraint is the duplicate-prevention mechanism.
CREATE TABLE IF NOT EXISTS discovered_documents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id           INTEGER NOT NULL REFERENCES sources(id),
    url                 TEXT    NOT NULL,
    link_text           TEXT,
    -- Page the link was found on, so a reviewer can retrace the discovery.
    found_on_url        TEXT,
    discovered_at       TEXT    NOT NULL,
    first_crawl_run_id  INTEGER REFERENCES crawl_runs(id),
    last_seen_at        TEXT    NOT NULL,
    -- Lifecycle: new -> downloaded -> parsed -> verified | rejected
    status              TEXT    NOT NULL DEFAULT 'new',
    status_reason       TEXT,
    UNIQUE (source_id, url),
    CHECK (status IN ('new', 'downloaded', 'parsed', 'verified', 'rejected'))
);

CREATE INDEX IF NOT EXISTS idx_discovered_status ON discovered_documents(status);

-- ---------------------------------------------------------------------------
-- Modules 3 & 4: Document Acquisition + Repository
-- ---------------------------------------------------------------------------
-- Write-once. A changed source document produces a NEW row with version+1 and
-- a link back via supersedes_id; the previous row and its bytes are retained.
CREATE TABLE IF NOT EXISTS documents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    discovered_id       INTEGER NOT NULL REFERENCES discovered_documents(id),
    source_id           INTEGER NOT NULL REFERENCES sources(id),
    source_url          TEXT    NOT NULL,
    file_name           TEXT    NOT NULL,     -- human-facing name, e.g. GO-123-2026.pdf
    -- Path relative to the repository root, content-addressed by hash.
    stored_path         TEXT    NOT NULL,
    sha256              TEXT    NOT NULL,
    byte_size           INTEGER NOT NULL,
    content_type        TEXT,
    http_status         INTEGER,
    etag                TEXT,
    last_modified       TEXT,
    downloaded_at       TEXT    NOT NULL,
    version             INTEGER NOT NULL DEFAULT 1,
    supersedes_id       INTEGER REFERENCES documents(id),
    UNIQUE (discovered_id, sha256)
);

CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(sha256);

-- ---------------------------------------------------------------------------
-- Module 5: PDF Text Extraction Engine
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extractions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id         INTEGER NOT NULL REFERENCES documents(id),
    backend             TEXT    NOT NULL,     -- pymupdf | pdfplumber | pypdf
    backend_version     TEXT,
    page_count          INTEGER NOT NULL,
    char_count          INTEGER NOT NULL,
    -- 0..1. Low confidence signals a scanned/image PDF needing OCR.
    confidence          REAL    NOT NULL,
    needs_ocr           INTEGER NOT NULL DEFAULT 0,
    log                 TEXT,
    extracted_at        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS extraction_pages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_id       INTEGER NOT NULL REFERENCES extractions(id),
    page_number         INTEGER NOT NULL,     -- 1-based, matches the printed PDF
    text                TEXT    NOT NULL,
    char_count          INTEGER NOT NULL,
    UNIQUE (extraction_id, page_number)
);

-- ---------------------------------------------------------------------------
-- Module 6: Metadata Extraction Engine
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS go_records (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_id       INTEGER NOT NULL REFERENCES extractions(id),
    document_id         INTEGER NOT NULL REFERENCES documents(id),
    source_id           INTEGER NOT NULL REFERENCES sources(id),
    extractor_version   TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'pending',
    reviewed_by         TEXT,
    reviewed_at         TEXT,
    review_note         TEXT,
    created_at          TEXT    NOT NULL,
    UNIQUE (extraction_id, extractor_version),
    CHECK (status IN ('pending', 'approved', 'rejected'))
);

CREATE INDEX IF NOT EXISTS idx_records_status ON go_records(status);

-- Evidence-bound field values. source_page and source_text are NOT NULL by
-- design: a value with no evidence is not representable in this schema.
-- Machine rows have origin='extracted'; a human correction inserts a new row
-- with origin='corrected' and supersedes the prior one.
CREATE TABLE IF NOT EXISTS go_fields (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id           INTEGER NOT NULL REFERENCES go_records(id),
    field_name          TEXT    NOT NULL,
    value               TEXT,
    normalized_value    TEXT,
    source_page         INTEGER NOT NULL,
    source_text         TEXT    NOT NULL,
    char_start          INTEGER,
    char_end            INTEGER,
    confidence          REAL    NOT NULL,
    method              TEXT    NOT NULL,     -- which pattern/rule produced it
    origin              TEXT    NOT NULL DEFAULT 'extracted',
    superseded_by       INTEGER REFERENCES go_fields(id),
    created_at          TEXT    NOT NULL,
    created_by          TEXT    NOT NULL DEFAULT 'system',
    CHECK (origin IN ('extracted', 'corrected')),
    CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

CREATE INDEX IF NOT EXISTS idx_fields_record ON go_fields(record_id, field_name);

-- Candidates that lost to the winning value. Kept so a reviewer can see what
-- else the extractor considered rather than only its final answer.
CREATE TABLE IF NOT EXISTS go_field_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id           INTEGER NOT NULL REFERENCES go_records(id),
    field_name          TEXT    NOT NULL,
    value               TEXT    NOT NULL,
    source_page         INTEGER NOT NULL,
    source_text         TEXT    NOT NULL,
    confidence          REAL    NOT NULL,
    method              TEXT    NOT NULL
);

-- ---------------------------------------------------------------------------
-- Module 8: Audit & Traceability Engine
-- ---------------------------------------------------------------------------
-- Append-only. Never UPDATE or DELETE rows in this table.
CREATE TABLE IF NOT EXISTS audit_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT    NOT NULL,
    actor               TEXT    NOT NULL,     -- 'system' or a reviewer id
    action              TEXT    NOT NULL,     -- e.g. document.downloaded
    entity_type         TEXT    NOT NULL,     -- source | discovered_document | ...
    entity_id           INTEGER,
    field_name          TEXT,
    before_value        TEXT,
    after_value         TEXT,
    detail              TEXT                  -- JSON blob for extra context
);

CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

-- Documents are the system of record: block any attempt to repoint or
-- re-fingerprint an archived file. New content must be a new row.
CREATE TRIGGER IF NOT EXISTS documents_immutable
BEFORE UPDATE OF sha256, stored_path, source_url, downloaded_at ON documents
BEGIN
    SELECT RAISE(ABORT, 'documents are write-once; insert a new version instead');
END;

CREATE TRIGGER IF NOT EXISTS documents_no_delete
BEFORE DELETE ON documents
BEGIN
    SELECT RAISE(ABORT, 'documents are write-once; originals must be retained');
END;
