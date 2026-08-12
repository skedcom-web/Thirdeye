"""Module 4 -- Document Repository.

The system of record. Files are content-addressed by SHA256 and written once:

    data/documents/<sha256[0:2]>/<sha256[2:4]>/<sha256>.pdf

Content addressing gives three properties the blueprint needs for free:
identical bytes are stored once, a file cannot be silently swapped (the path
IS the fingerprint), and integrity is verifiable by rehashing.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import audit
from .config import Settings
from .db import utcnow


class RepositoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredFile:
    sha256: str
    relative_path: str
    absolute_path: Path
    byte_size: int
    deduplicated: bool


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def relative_path_for(digest: str, suffix: str = ".pdf") -> str:
    return f"{digest[0:2]}/{digest[2:4]}/{digest}{suffix}"


def store(settings: Settings, payload: bytes, *, suffix: str = ".pdf") -> StoredFile:
    """Write bytes into the repository. Never overwrites an existing file."""
    digest = sha256_bytes(payload)
    relative = relative_path_for(digest, suffix)
    absolute = settings.repository_dir / relative
    absolute.parent.mkdir(parents=True, exist_ok=True)

    if absolute.exists():
        # Same digest means same bytes. Verify rather than assume, so a
        # corrupted or tampered archive surfaces here instead of downstream.
        existing_digest = sha256_file(absolute)
        if existing_digest != digest:
            raise RepositoryError(
                f"repository corruption: {relative} hashes to {existing_digest}, expected {digest}"
            )
        return StoredFile(digest, relative, absolute, len(payload), deduplicated=True)

    # Write to a temp name and move into place, so an interrupted write can
    # never leave a truncated file sitting at a content-addressed path.
    temp_path = absolute.with_suffix(absolute.suffix + ".part")
    temp_path.write_bytes(payload)
    temp_path.replace(absolute)
    return StoredFile(digest, relative, absolute, len(payload), deduplicated=False)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def absolute_path(settings: Settings, relative_path: str) -> Path:
    return settings.repository_dir / relative_path


def verify_document(
    settings: Settings, conn: sqlite3.Connection, document_id: int
) -> tuple[bool, str]:
    """Re-hash an archived file and compare it to the recorded fingerprint."""
    row = conn.execute(
        "SELECT stored_path, sha256 FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None:
        return False, "document not found"
    path = absolute_path(settings, row["stored_path"])
    if not path.exists():
        return False, f"file missing from repository: {row['stored_path']}"
    actual = sha256_file(path)
    if actual != row["sha256"]:
        return False, f"hash mismatch: stored {actual}, expected {row['sha256']}"
    return True, "ok"


def verify_all(settings: Settings, conn: sqlite3.Connection) -> list[tuple[int, bool, str]]:
    """Integrity sweep across the whole repository."""
    ids = [int(r["id"]) for r in conn.execute("SELECT id FROM documents ORDER BY id").fetchall()]
    results = []
    for document_id in ids:
        ok, message = verify_document(settings, conn, document_id)
        results.append((document_id, ok, message))
        if not ok:
            audit.record(
                conn,
                action="repository.integrity_failed",
                entity_type="document",
                entity_id=document_id,
                detail={"message": message},
            )
    return results


def version_history(conn: sqlite3.Connection, discovered_id: int) -> list[sqlite3.Row]:
    """All archived versions of one source URL, oldest first."""
    return conn.execute(
        """
        SELECT id, version, sha256, byte_size, downloaded_at, stored_path, supersedes_id
          FROM documents
         WHERE discovered_id = ?
         ORDER BY version
        """,
        (discovered_id,),
    ).fetchall()


def stats(settings: Settings, conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(byte_size), 0) AS bytes,"
        " COUNT(DISTINCT sha256) AS unique_files FROM documents"
    ).fetchone()
    return {
        "documents": int(row["n"]),
        "unique_files": int(row["unique_files"]),
        "total_bytes": int(row["bytes"]),
    }


def record_document(
    conn: sqlite3.Connection,
    *,
    discovered_id: int,
    source_id: int,
    source_url: str,
    file_name: str,
    stored: StoredFile,
    content_type: str | None,
    http_status: int | None,
    etag: str | None = None,
    last_modified: str | None = None,
    actor: str = audit.SYSTEM_ACTOR,
) -> tuple[int, bool]:
    """Register an archived file. Returns (document_id, is_new_version).

    If this URL was archived before with different bytes, the source document
    changed: a new version row is written and the previous one is retained and
    linked, per the never-overwrite rule.
    """
    prior = conn.execute(
        """
        SELECT id, version, sha256 FROM documents
         WHERE discovered_id = ?
         ORDER BY version DESC
         LIMIT 1
        """,
        (discovered_id,),
    ).fetchone()

    if prior is not None and prior["sha256"] == stored.sha256:
        # Byte-identical re-download: nothing changed, keep the existing row.
        audit.record(
            conn,
            action="document.unchanged",
            entity_type="document",
            entity_id=int(prior["id"]),
            actor=actor,
            detail={"sha256": stored.sha256},
        )
        return int(prior["id"]), False

    version = int(prior["version"]) + 1 if prior is not None else 1
    supersedes_id = int(prior["id"]) if prior is not None else None

    cur = conn.execute(
        """
        INSERT INTO documents
            (discovered_id, source_id, source_url, file_name, stored_path, sha256,
             byte_size, content_type, http_status, etag, last_modified,
             downloaded_at, version, supersedes_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            discovered_id,
            source_id,
            source_url,
            file_name,
            stored.relative_path,
            stored.sha256,
            stored.byte_size,
            content_type,
            http_status,
            etag,
            last_modified,
            utcnow(),
            version,
            supersedes_id,
        ),
    )
    document_id = int(cur.lastrowid)

    audit.record(
        conn,
        action="document.downloaded" if version == 1 else "document.new_version",
        entity_type="document",
        entity_id=document_id,
        actor=actor,
        field_name="sha256" if version > 1 else None,
        before_value=prior["sha256"] if prior is not None else None,
        after_value=stored.sha256,
        detail={
            "source_url": source_url,
            "file_name": file_name,
            "stored_path": stored.relative_path,
            "byte_size": stored.byte_size,
            "version": version,
            "deduplicated_bytes": stored.deduplicated,
        },
    )
    return document_id, True
