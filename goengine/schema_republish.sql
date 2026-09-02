-- Phase 3.9 Initiatives 6 & 7 -- Published GO Edit & Republish Workflow.
-- One row per requested revision to an already-published go_record. Never
-- touches go_records.status (which stays 'approved' throughout, so the
-- citizen-facing record never disappears mid-review) and never carries
-- go_number/go_date/department changes -- those are frozen once published
-- (see operations/republish.py's docstring for why: go_identity.py's
-- compute_identity() would otherwise silently rewrite the permanent URL).
-- Field-level before/after values are recorded in go_fields itself via the
-- existing review.correct_field() at approval time; `changes` here is a
-- snapshot of what was *requested*, for the pending-review UI to show.
CREATE TABLE IF NOT EXISTS go_record_revisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id      INTEGER NOT NULL REFERENCES go_records(id),
    version        INTEGER NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'REVISION_DRAFT',
    requested_by   TEXT    NOT NULL,
    requested_at   TEXT    NOT NULL,
    reason         TEXT    NOT NULL,
    changes        TEXT    NOT NULL,
    reviewed_by    TEXT,
    reviewed_at    TEXT,
    review_note    TEXT,
    republished_at TEXT,
    CHECK (status IN ('REVISION_DRAFT', 'REVISION_PENDING_REVIEW', 'REPUBLISHED', 'REJECTED'))
);

CREATE INDEX IF NOT EXISTS idx_go_record_revisions_record ON go_record_revisions(record_id, version);
