"""Phase 4A -- Citizen accounts, sessions, saved searches, bookmarks, and
download history.

A fully separate identity system from `operations/auth.py`'s staff
`users`/`sessions` -- see `goengine/schema_citizen.sql`'s header comment for
why. Password hashing and the session-token shape are copied in spirit from
`auth.py` (same `hash_password`/`verify_password`, reused directly -- they
take no DB dependency) so citizen accounts get the same real security
properties as staff accounts, just on their own tables and cookie.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import audit, public
from ..db import utcnow
from . import auth

SESSION_TTL_HOURS = 24

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CitizenError(ValueError):
    pass


@dataclass(frozen=True)
class CitizenUser:
    id: int
    full_name: str
    email: str
    mobile: str | None
    active: bool


def _row_to_citizen(row: sqlite3.Row) -> CitizenUser:
    return CitizenUser(
        id=int(row["id"]), full_name=row["full_name"], email=row["email"],
        mobile=row["mobile"], active=bool(row["active"]),
    )


# ---------------------------------------------------------------------------
# Registration / accounts
# ---------------------------------------------------------------------------
def register(
    conn: sqlite3.Connection,
    *,
    full_name: str,
    email: str,
    mobile: str | None,
    password: str,
    terms_accepted: bool,
    actor: str = "self-registration",
) -> int:
    full_name = (full_name or "").strip()
    email = (email or "").strip().lower()
    if not full_name:
        raise CitizenError("full name is required")
    if not _EMAIL_RE.match(email):
        raise CitizenError("a valid email address is required")
    if len(password) < 8:
        raise CitizenError("password must be at least 8 characters")
    if not terms_accepted:
        raise CitizenError("you must accept the terms to register")

    existing = conn.execute("SELECT id FROM citizen_users WHERE email = ?", (email,)).fetchone()
    if existing is not None:
        raise CitizenError(f"an account with email {email!r} already exists")

    now = utcnow()
    cur = conn.execute(
        """
        INSERT INTO citizen_users
            (full_name, email, mobile, password_hash, terms_accepted_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (full_name, email, mobile or None, auth.hash_password(password), now, now),
    )
    citizen_id = int(cur.lastrowid)
    audit.record(
        conn, action="citizen.registered", entity_type="citizen_user", entity_id=citizen_id,
        actor=actor, detail={"email": email},
    )
    return citizen_id


def get_citizen(conn: sqlite3.Connection, citizen_id: int) -> CitizenUser | None:
    row = conn.execute("SELECT * FROM citizen_users WHERE id = ?", (citizen_id,)).fetchone()
    return _row_to_citizen(row) if row else None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def authenticate(conn: sqlite3.Connection, email: str, password: str) -> CitizenUser | None:
    row = conn.execute(
        "SELECT * FROM citizen_users WHERE email = ? AND active = 1", ((email or "").strip().lower(),)
    ).fetchone()
    if row is None or not auth.verify_password(password, row["password_hash"]):
        return None
    return _row_to_citizen(row)


def create_session(conn: sqlite3.Connection, citizen: CitizenUser, *, ttl_hours: int = SESSION_TTL_HOURS) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=ttl_hours)
    conn.execute(
        "INSERT INTO citizen_sessions (token, citizen_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, citizen.id, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")),
    )
    audit.record(conn, action="citizen.logged_in", entity_type="citizen_user", entity_id=citizen.id, actor=citizen.email)
    return token


def get_session_user(conn: sqlite3.Connection, token: str | None) -> CitizenUser | None:
    if not token:
        return None
    row = conn.execute(
        """
        SELECT c.* FROM citizen_sessions s JOIN citizen_users c ON c.id = s.citizen_id
         WHERE s.token = ? AND s.expires_at > ? AND c.active = 1
        """,
        (token, utcnow()),
    ).fetchone()
    return _row_to_citizen(row) if row else None


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    row = conn.execute("SELECT citizen_id FROM citizen_sessions WHERE token = ?", (token,)).fetchone()
    conn.execute("DELETE FROM citizen_sessions WHERE token = ?", (token,))
    if row is not None:
        citizen = get_citizen(conn, int(row["citizen_id"]))
        if citizen is not None:
            audit.record(conn, action="citizen.logged_out", entity_type="citizen_user", entity_id=citizen.id, actor=citizen.email)


# ---------------------------------------------------------------------------
# Saved searches
# ---------------------------------------------------------------------------
def save_search(
    conn: sqlite3.Connection,
    citizen_id: int,
    *,
    label: str | None = None,
    department_bucket: str | None = None,
    district: str | None = None,
    q: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO saved_searches (citizen_id, label, department_bucket, district, q, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (citizen_id, label or None, department_bucket or None, district or None, q or None, utcnow()),
    )
    return int(cur.lastrowid)


def list_saved_searches(conn: sqlite3.Connection, citizen_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM saved_searches WHERE citizen_id = ? ORDER BY id DESC",
        (citizen_id,),
    ).fetchall()


def delete_saved_search(conn: sqlite3.Connection, citizen_id: int, search_id: int) -> None:
    conn.execute(
        "DELETE FROM saved_searches WHERE id = ? AND citizen_id = ?", (search_id, citizen_id)
    )


# ---------------------------------------------------------------------------
# Bookmarks (saved records)
# ---------------------------------------------------------------------------
def is_saved(conn: sqlite3.Connection, citizen_id: int, record_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM saved_records WHERE citizen_id = ? AND record_id = ?",
        (citizen_id, record_id),
    ).fetchone()
    return row is not None


def toggle_saved_record(conn: sqlite3.Connection, citizen_id: int, record_id: int) -> bool:
    """Returns the new saved state (True if now saved, False if now removed)."""
    if is_saved(conn, citizen_id, record_id):
        conn.execute(
            "DELETE FROM saved_records WHERE citizen_id = ? AND record_id = ?",
            (citizen_id, record_id),
        )
        return False
    conn.execute(
        "INSERT INTO saved_records (citizen_id, record_id, created_at) VALUES (?, ?, ?)",
        (citizen_id, record_id, utcnow()),
    )
    return True


def list_saved_records(conn: sqlite3.Connection, citizen_id: int, limit: int = 20) -> list[public.PublicRecord]:
    rows = conn.execute(
        "SELECT record_id FROM saved_records WHERE citizen_id = ? ORDER BY created_at DESC LIMIT ?",
        (citizen_id, limit),
    ).fetchall()
    records = [public.get(conn, int(r["record_id"])) for r in rows]
    return [r for r in records if r is not None]


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------
def log_download(
    conn: sqlite3.Connection,
    *,
    record_id: int,
    format: str,
    citizen_id: int | None = None,
    staff_user_id: int | None = None,
) -> None:
    if (citizen_id is None) == (staff_user_id is None):
        raise CitizenError("exactly one of citizen_id/staff_user_id must be set")
    conn.execute(
        """
        INSERT INTO download_log (citizen_id, staff_user_id, record_id, format, downloaded_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (citizen_id, staff_user_id, record_id, format, utcnow()),
    )


def recent_downloads(conn: sqlite3.Connection, citizen_id: int, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        """
        SELECT d.record_id, d.format, d.downloaded_at,
               f_num.normalized_value AS go_number, f_sub.normalized_value AS subject
          FROM download_log d
          LEFT JOIN go_fields f_num
                 ON f_num.record_id = d.record_id AND f_num.field_name = 'go_number' AND f_num.superseded_by IS NULL
          LEFT JOIN go_fields f_sub
                 ON f_sub.record_id = d.record_id AND f_sub.field_name = 'subject' AND f_sub.superseded_by IS NULL
         WHERE d.citizen_id = ?
         ORDER BY d.downloaded_at DESC, d.id DESC
         LIMIT ?
        """,
        (citizen_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
