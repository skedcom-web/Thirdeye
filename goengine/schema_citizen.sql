-- Phase 4A -- Citizen Experience Blueprint.
--
-- Deliberately a fully separate identity system from `users`/`sessions`
-- (staff), not a new role value on the existing tables: `users.role` has a
-- CHECK constraint SQLite can't extend without rebuilding the table, and
-- that table holds the live admin credentials. These tables are purely
-- additive and share nothing with staff auth except the hashing code in
-- operations/auth.py (hash_password/verify_password take no DB dependency).

CREATE TABLE IF NOT EXISTS citizen_users (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name          TEXT    NOT NULL,
    email              TEXT    NOT NULL UNIQUE,
    mobile             TEXT,
    password_hash      TEXT    NOT NULL,
    terms_accepted_at  TEXT    NOT NULL,
    active             INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS citizen_sessions (
    token       TEXT    PRIMARY KEY,
    citizen_id  INTEGER NOT NULL REFERENCES citizen_users(id),
    created_at  TEXT    NOT NULL,
    expires_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_citizen_sessions_citizen ON citizen_sessions(citizen_id);

CREATE TABLE IF NOT EXISTS saved_searches (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    citizen_id         INTEGER NOT NULL REFERENCES citizen_users(id),
    label              TEXT,
    department_bucket  TEXT,
    district           TEXT,
    q                  TEXT,
    created_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_saved_searches_citizen ON saved_searches(citizen_id);

-- Bookmarks. A citizen either has a record saved or doesn't -- no history of
-- toggling, so the natural key is the pair itself.
CREATE TABLE IF NOT EXISTS saved_records (
    citizen_id  INTEGER NOT NULL REFERENCES citizen_users(id),
    record_id   INTEGER NOT NULL REFERENCES go_records(id),
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (citizen_id, record_id)
);

-- Every successful download, by citizen or staff -- exactly one of
-- citizen_id/staff_user_id is set, enforced below rather than left as an
-- undocumented convention.
CREATE TABLE IF NOT EXISTS download_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    citizen_id     INTEGER REFERENCES citizen_users(id),
    staff_user_id  INTEGER REFERENCES users(id),
    record_id      INTEGER NOT NULL REFERENCES go_records(id),
    format         TEXT    NOT NULL,   -- pdf | text | metadata
    downloaded_at  TEXT    NOT NULL,
    CHECK ((citizen_id IS NULL) != (staff_user_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_download_log_citizen ON download_log(citizen_id);
