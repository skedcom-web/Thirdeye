"""Phase 3.4 -- Local Extraction Agent credentials.

A separate module from `operations/auth.py` on purpose: that file is Module
11's session-cookie, five-role auth for human operators, and a machine
bearer credential doesn't fit that model -- mixing them would raise the
blast radius of the file every other module already imports.

Token hashing uses plain SHA-256, not scrypt: `auth.py`'s `hash_password`
deliberately uses a slow KDF because human passwords have low entropy and
are guessable. A `secrets.token_urlsafe(32)` agent key has ~256 bits of
entropy already -- nothing to slow an attacker down from, and a slow hash
would only add latency to every authenticated sync request.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass

from .. import audit
from ..db import utcnow

TOKEN_PREFIX = "tea_"  # Third Eye Agent


@dataclass(frozen=True)
class AgentKey:
    id: int
    label: str
    key_prefix: str
    created_by: str
    created_at: str
    revoked_at: str | None
    last_used_at: str | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def hash_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row_to_agent_key(row: sqlite3.Row) -> AgentKey:
    return AgentKey(
        id=int(row["id"]),
        label=row["label"],
        key_prefix=row["key_prefix"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
        last_used_at=row["last_used_at"],
    )


def generate_key(conn: sqlite3.Connection, *, label: str, created_by: str) -> tuple[int, str]:
    """Creates a new agent key. Returns (agent_key_id, plaintext_token) --
    the plaintext is never stored or logged again after this call returns;
    only its hash and a display prefix persist."""
    if not label or not label.strip():
        raise ValueError("label is required")

    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = utcnow()
    cur = conn.execute(
        """
        INSERT INTO agent_keys (label, key_hash, key_prefix, created_by, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (label.strip(), hash_key(token), token[:12], created_by, now),
    )
    agent_key_id = int(cur.lastrowid)
    audit.record(
        conn, action="agent_key.created", entity_type="agent_key", entity_id=agent_key_id,
        actor=created_by, detail={"label": label.strip()},
    )
    return agent_key_id, token


def verify_key(conn: sqlite3.Connection, token: str) -> AgentKey | None:
    """Returns the matching active AgentKey, or None if the token is unknown
    or revoked. Updates last_used_at as a side effect on a successful match."""
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM agent_keys WHERE key_hash = ?", (hash_key(token),)
    ).fetchone()
    if row is None or row["revoked_at"] is not None:
        return None
    conn.execute(
        "UPDATE agent_keys SET last_used_at = ? WHERE id = ?", (utcnow(), int(row["id"]))
    )
    return _row_to_agent_key(row)


def revoke_key(conn: sqlite3.Connection, agent_key_id: int, *, actor: str) -> None:
    conn.execute(
        "UPDATE agent_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (utcnow(), agent_key_id),
    )
    audit.record(
        conn, action="agent_key.revoked", entity_type="agent_key", entity_id=agent_key_id, actor=actor,
    )


def ensure_key(conn: sqlite3.Connection, token: str, *, label: str, created_by: str) -> None:
    """Idempotently guarantees a specific, already-known token (as opposed
    to generate_key's freshly-random one) is a valid active key. For
    THIRDEYE_BOOTSTRAP_AGENT_KEY: Render environment variables survive a
    redeploy even though the database doesn't, so calling this on every
    startup means a local agent's saved key never goes stale after a reset.

    No-ops if a row for this token already exists -- even if revoked, so a
    deliberate revocation within the same database's lifetime is never
    silently undone. It only matters right after a fresh reset, when no row
    exists at all.
    """
    existing = conn.execute("SELECT id FROM agent_keys WHERE key_hash = ?", (hash_key(token),)).fetchone()
    if existing is not None:
        return
    cur = conn.execute(
        "INSERT INTO agent_keys (label, key_hash, key_prefix, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (label, hash_key(token), token[:12], created_by, utcnow()),
    )
    audit.record(
        conn, action="agent_key.bootstrapped", entity_type="agent_key", entity_id=int(cur.lastrowid),
        actor=created_by, detail={"label": label},
    )


def list_keys(conn: sqlite3.Connection) -> list[AgentKey]:
    rows = conn.execute("SELECT * FROM agent_keys ORDER BY id DESC").fetchall()
    return [_row_to_agent_key(r) for r in rows]


def get_key(conn: sqlite3.Connection, agent_key_id: int) -> AgentKey | None:
    row = conn.execute("SELECT * FROM agent_keys WHERE id = ?", (agent_key_id,)).fetchone()
    return _row_to_agent_key(row) if row is not None else None
