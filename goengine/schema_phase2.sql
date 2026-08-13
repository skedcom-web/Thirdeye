-- Thirdeye GO Intelligence Engine -- Phase 2 schema (certification).
--
-- Additive to schema.sql: every statement is idempotent (CREATE ... IF NOT
-- EXISTS) so running this against a Phase 1 database only adds new tables.
-- New columns on the pre-existing `sources` table are added separately in
-- db.py via introspection, since SQLite ALTER TABLE has no IF NOT EXISTS.
--
-- Governance rules enforced structurally here:
--   * A golden document references a real, archived `documents` row via a
--     foreign key -- a synthetic fixture that never went through acquisition
--     cannot become part of the certification benchmark.
--   * A golden annotation is never UPDATEd; a correction supersedes, the
--     same pattern as go_fields, so the original human judgement survives.
--   * Every scored mismatch becomes a permanent extraction_failures row.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Module 1: Source Certification Engine
-- ---------------------------------------------------------------------------
-- One row per certification run. `sources.certification_status` (added via
-- migration) always mirrors the most recent row's result, so the registry
-- can be queried without a join for the common case.
CREATE TABLE IF NOT EXISTS source_certifications (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id             INTEGER NOT NULL REFERENCES sources(id),
    started_at            TEXT    NOT NULL,
    finished_at           TEXT,
    result                TEXT    NOT NULL,   -- CERTIFIED | PARTIALLY_CERTIFIED | FAILED
    connectivity_ok       INTEGER,
    discovery_ok          INTEGER,
    download_ok           INTEGER,
    stability_ok          INTEGER,
    authenticity_ok       INTEGER,
    documents_discovered  INTEGER NOT NULL DEFAULT 0,
    documents_downloaded  INTEGER NOT NULL DEFAULT 0,
    detail                TEXT,               -- JSON: per-check messages
    actor                 TEXT    NOT NULL DEFAULT 'system',
    CHECK (result IN ('CERTIFIED', 'PARTIALLY_CERTIFIED', 'FAILED'))
);

CREATE INDEX IF NOT EXISTS idx_certifications_source ON source_certifications(source_id);

-- ---------------------------------------------------------------------------
-- Modules 2 & 5: Document categorization + language classification
-- ---------------------------------------------------------------------------
-- Computed automatically at parse time; one row per document.
CREATE TABLE IF NOT EXISTS document_categories (
    document_id          INTEGER PRIMARY KEY REFERENCES documents(id),
    text_type             TEXT    NOT NULL,   -- digital | scanned
    language               TEXT    NOT NULL,   -- english | tamil | mixed | unknown
    tamil_char_ratio         REAL,
    english_char_ratio       REAL,
    annexure_heavy             INTEGER NOT NULL DEFAULT 0,
    table_heavy                 INTEGER NOT NULL DEFAULT 0,
    department_bucket             TEXT,        -- health|education|public_works|rural_development|other
    page_count                     INTEGER NOT NULL,
    computed_at                      TEXT    NOT NULL,
    CHECK (text_type IN ('digital', 'scanned')),
    CHECK (language IN ('english', 'tamil', 'mixed', 'unknown')),
    CHECK (department_bucket IN
           ('health', 'education', 'public_works', 'rural_development', 'other'))
);

CREATE INDEX IF NOT EXISTS idx_categories_department ON document_categories(department_bucket);
CREATE INDEX IF NOT EXISTS idx_categories_language ON document_categories(language);

-- ---------------------------------------------------------------------------
-- Module 4: OCR Intelligence Engine
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ocr_runs (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_id          INTEGER NOT NULL REFERENCES extractions(id),
    engine                  TEXT    NOT NULL DEFAULT 'tesseract',
    engine_version            TEXT,
    languages                  TEXT    NOT NULL,  -- e.g. 'eng+tam'
    pages_ocred                  INTEGER NOT NULL,
    mean_word_confidence           REAL,
    ran_at                           TEXT    NOT NULL,
    log                               TEXT
);

CREATE TABLE IF NOT EXISTS ocr_pages (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    ocr_run_id             INTEGER NOT NULL REFERENCES ocr_runs(id),
    page_number             INTEGER NOT NULL,
    text                     TEXT    NOT NULL,
    mean_word_confidence      REAL,
    UNIQUE (ocr_run_id, page_number)
);

-- ---------------------------------------------------------------------------
-- Module 3: Golden Dataset Workbench (real documents only)
-- ---------------------------------------------------------------------------
-- FK to `documents(id)` is the structural guarantee behind governance rule 3
-- ("no benchmarking against synthetic data"): a document only exists in this
-- table if it passed through real discovery + acquisition (Modules 2-4).
CREATE TABLE IF NOT EXISTS golden_documents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id          INTEGER NOT NULL UNIQUE REFERENCES documents(id),
    added_by               TEXT    NOT NULL,
    added_at                 TEXT    NOT NULL,
    notes                     TEXT
);

-- Append-only ground truth. A blank/NULL value is the annotator asserting
-- the field genuinely does not appear in the document -- distinct from "not
-- yet annotated" (no row at all for that field).
CREATE TABLE IF NOT EXISTS golden_annotations (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    golden_document_id     INTEGER NOT NULL REFERENCES golden_documents(id),
    field_name              TEXT    NOT NULL,
    value                     TEXT,
    annotator                  TEXT    NOT NULL,
    annotated_at                 TEXT    NOT NULL,
    note                           TEXT,
    superseded_by                    INTEGER REFERENCES golden_annotations(id),
    CHECK (field_name IN
           ('go_number', 'go_date', 'department', 'subject', 'budget',
            'district', 'scheme_name', 'project_type'))
);

CREATE INDEX IF NOT EXISTS idx_golden_annotations_doc
    ON golden_annotations(golden_document_id, field_name);

-- ---------------------------------------------------------------------------
-- Module 6: Benchmark & Accuracy Engine
-- ---------------------------------------------------------------------------
-- Persisted so the certification dashboard and calibration module have
-- history to show, not just the result of the most recent run.
CREATE TABLE IF NOT EXISTS certification_benchmark_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at                TEXT    NOT NULL,
    extractor_version       TEXT    NOT NULL,
    documents_scored          INTEGER NOT NULL,
    summary                     TEXT    NOT NULL  -- JSON: full report (P/R/F1 per field/dept/lang)
);

-- ---------------------------------------------------------------------------
-- Module 7: Failure Intelligence Engine
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extraction_failures (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    benchmark_run_id      INTEGER NOT NULL REFERENCES certification_benchmark_runs(id),
    document_id             INTEGER NOT NULL REFERENCES documents(id),
    field_name                TEXT    NOT NULL,
    expected_value               TEXT,
    actual_value                    TEXT,
    failure_type                      TEXT    NOT NULL,
    department_bucket                   TEXT,
    language                              TEXT,
    created_at                              TEXT    NOT NULL,
    CHECK (failure_type IN
           ('ocr_failure', 'budget_failure', 'district_failure',
            'tamil_parsing_failure', 'reference_misclassification',
            'table_extraction_failure', 'hallucination', 'other'))
);

CREATE INDEX IF NOT EXISTS idx_failures_type ON extraction_failures(failure_type);
CREATE INDEX IF NOT EXISTS idx_failures_field ON extraction_failures(field_name);
CREATE INDEX IF NOT EXISTS idx_failures_run ON extraction_failures(benchmark_run_id);

-- ---------------------------------------------------------------------------
-- Module 8: Extraction Confidence Calibration
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calibration_snapshots (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    benchmark_run_id       INTEGER NOT NULL REFERENCES certification_benchmark_runs(id),
    field_name               TEXT    NOT NULL,
    bucket_low                 REAL    NOT NULL,
    bucket_high                   REAL    NOT NULL,
    predictions_count                INTEGER NOT NULL,
    correct_count                       INTEGER NOT NULL,
    mean_stated_confidence                 REAL    NOT NULL,
    actual_accuracy                           REAL    NOT NULL,
    calibration_gap                              REAL    NOT NULL  -- actual - stated
);

CREATE INDEX IF NOT EXISTS idx_calibration_run ON calibration_snapshots(benchmark_run_id);

-- Golden data is never destroyed, matching go_fields' correction pattern.
CREATE TRIGGER IF NOT EXISTS golden_annotations_no_delete
BEFORE DELETE ON golden_annotations
BEGIN
    SELECT RAISE(ABORT, 'golden_annotations is append-only; supersede instead of deleting');
END;

CREATE TRIGGER IF NOT EXISTS golden_documents_no_delete
BEFORE DELETE ON golden_documents
BEGIN
    SELECT RAISE(ABORT, 'a document cannot be removed from the certified golden set');
END;
