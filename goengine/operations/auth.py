"""Module 11 -- User & Role Management.

Session-cookie authentication with five roles, no external auth dependency:
password hashing uses `hashlib.scrypt` (stdlib since Python 3.6) rather than
pulling in bcrypt/passlib, matching this project's pattern of minimal new
dependencies. This is a real login system suitable for a small operator
team, not an enterprise IdP -- there is no SSO, no MFA, no password reset
flow. Good enough to make every write action attributable to a real
authenticated identity instead of Phase 1/2's free-text reviewer name field,
which was flagged as a known gap in both of those phases' READMEs.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import audit
from ..db import utcnow

ROLE_PLATFORM_ADMIN = "platform_admin"
ROLE_STATE_ADMIN = "state_admin"
ROLE_REVIEWER = "reviewer"
ROLE_AUDITOR = "auditor"
ROLE_READ_ONLY = "read_only"

ALL_ROLES = (ROLE_PLATFORM_ADMIN, ROLE_STATE_ADMIN, ROLE_REVIEWER, ROLE_AUDITOR, ROLE_READ_ONLY)

# Named permissions, and which roles hold them. Read access (viewing any
# page) is available to every authenticated role including read_only;
# everything below is a WRITE permission.
PERM_MANAGE_USERS = "manage_users"
PERM_MANAGE_STATES = "manage_states"
PERM_MANAGE_DEPARTMENTS = "manage_departments"
PERM_MANAGE_DISTRICTS = "manage_districts"
PERM_MANAGE_SOURCES = "manage_sources"
PERM_RUN_CERTIFICATION = "run_certification"
PERM_REVIEW_RECORDS = "review_records"
PERM_ESCALATE_RECORDS = "escalate_records"
PERM_PUBLISH = "publish"

_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_PLATFORM_ADMIN: frozenset(
        {
            PERM_MANAGE_USERS, PERM_MANAGE_STATES, PERM_MANAGE_DEPARTMENTS,
            PERM_MANAGE_DISTRICTS, PERM_MANAGE_SOURCES, PERM_RUN_CERTIFICATION,
            PERM_REVIEW_RECORDS, PERM_ESCALATE_RECORDS, PERM_PUBLISH,
        }
    ),
    # State admins manage everything within their own state, but not the
    # platform-wide state list or department master list, and not other users.
    ROLE_STATE_ADMIN: frozenset(
        {
            PERM_MANAGE_DISTRICTS, PERM_MANAGE_SOURCES, PERM_RUN_CERTIFICATION,
            PERM_REVIEW_RECORDS, PERM_ESCALATE_RECORDS, PERM_PUBLISH,
        }
    ),
    ROLE_REVIEWER: frozenset({PERM_REVIEW_RECORDS, PERM_ESCALATE_RECORDS}),
    ROLE_AUDITOR: frozenset(),
    ROLE_READ_ONLY: frozenset(),
}

SESSION_TTL_HOURS = 24


class AuthError(ValueError):
    pass


class PermissionDenied(PermissionError):
    pass


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str
    state_id: int | None
    active: bool

    def has_permission(self, permission: str) -> bool:
        return self.active and permission in _ROLE_PERMISSIONS.get(self.role, frozenset())

    def can_act_on_state(self, state_id: int | None) -> bool:
        """Platform admins and non-state-scoped roles act everywhere; a
        state admin only within their assigned state."""
        if self.role != ROLE_STATE_ADMIN:
            return True
        return state_id is not None and state_id == self.state_id


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(derived, expected)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def has_any_users(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def create_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    password: str,
    role: str,
    state_id: int | None = None,
    actor: str = audit.SYSTEM_ACTOR,
) -> int:
    if role not in ALL_ROLES:
        raise AuthError(f"role must be one of {ALL_ROLES}")
    if role == ROLE_STATE_ADMIN and state_id is None:
        raise AuthError("a state_admin must be assigned a state")
    if not username or not username.strip():
        raise AuthError("username is required")
    if len(password) < 8:
        raise AuthError("password must be at least 8 characters")

    existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing is not None:
        raise AuthError(f"username {username!r} is already taken")

    cur = conn.execute(
        """
        INSERT INTO users (username, password_hash, role, state_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (username, hash_password(password), role, state_id, utcnow()),
    )
    user_id = int(cur.lastrowid)
    audit.record(
        conn, action="user.created", entity_type="user", entity_id=user_id, actor=actor,
        detail={"username": username, "role": role, "state_id": state_id},
    )
    return user_id


def set_user_active(
    conn: sqlite3.Connection, user_id: int, active: bool, *, actor: str
) -> None:
    row = conn.execute("SELECT active FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise LookupError(f"no user with id {user_id}")
    conn.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))
    audit.record(
        conn, action="user.activated" if active else "user.deactivated",
        entity_type="user", entity_id=user_id, actor=actor,
        field_name="active", before_value=bool(row["active"]), after_value=active,
    )


def get_user(conn: sqlite3.Connection, user_id: int) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def list_users(conn: sqlite3.Connection) -> list[User]:
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [_row_to_user(r) for r in rows]


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=int(row["id"]), username=row["username"], role=row["role"],
        state_id=row["state_id"], active=bool(row["active"]),
    )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def authenticate(conn: sqlite3.Connection, username: str, password: str) -> User | None:
    row = conn.execute(
        "SELECT * FROM users WHERE username = ? AND active = 1", (username,)
    ).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    return _row_to_user(row)


def create_session(conn: sqlite3.Connection, user: User, *, ttl_hours: int = SESSION_TTL_HOURS) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=ttl_hours)
    conn.execute(
        "INSERT INTO sessions (id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user.id, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")),
    )
    audit.record(conn, action="user.logged_in", entity_type="user", entity_id=user.id, actor=user.username)
    return token


def get_session_user(conn: sqlite3.Connection, token: str | None) -> User | None:
    if not token:
        return None
    row = conn.execute(
        """
        SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
         WHERE s.id = ? AND s.expires_at > ? AND u.active = 1
        """,
        (token, utcnow()),
    ).fetchone()
    return _row_to_user(row) if row else None


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    row = conn.execute("SELECT user_id FROM sessions WHERE id = ?", (token,)).fetchone()
    conn.execute("DELETE FROM sessions WHERE id = ?", (token,))
    if row is not None:
        user = get_user(conn, int(row["user_id"]))
        if user is not None:
            audit.record(conn, action="user.logged_out", entity_type="user", entity_id=user.id, actor=user.username)


def purge_expired_sessions(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (utcnow(),))
    return cur.rowcount
