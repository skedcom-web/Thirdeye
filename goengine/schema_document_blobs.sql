-- Durable off-disk storage for archived PDF bytes.
--
-- Render's disk -- even the persistent one configured in render.yaml -- is
-- not the source of truth for a document's bytes, only scratch space the
-- parse pipeline reads from while a sync is in progress. Whatever a Local
-- Extraction Agent uploads to /api/agent/sync/document is additionally
-- written here, into whichever database `conn` is connected to (local
-- SQLite in dev/tests, Turso in production -- same table, same code, see
-- turso_db.py), so a disk reset can never again turn a live document row
-- into a "file missing from repository" error. See repository.py's
-- store_blob/read_blob/read_bytes for the read/write helpers, and
-- agent_routes.py for where a sync writes one.

CREATE TABLE IF NOT EXISTS document_blobs (
    document_id  INTEGER PRIMARY KEY REFERENCES documents(id),
    data         BLOB    NOT NULL,
    byte_size    INTEGER NOT NULL,
    created_at   TEXT    NOT NULL
);
