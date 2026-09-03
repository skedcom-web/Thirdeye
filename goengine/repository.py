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


def store_blob(conn: sqlite3.Connection, document_id: int, payload: bytes) -> None:
    """The durable copy of an archived document's bytes -- written to
    whatever `conn` is connected to (local SQLite in dev/tests, Turso in
    production; see turso_db.py), unlike `store()` above which only ever
    writes to local disk. Local disk (even Render's persistent disk) is
    scratch space the parse pipeline reads from mid-sync, not something a
    citizen's download should ever depend on surviving a redeploy -- see
    read_bytes() below for how the two are reconciled during the rollout of
    this table on documents synced before it existed."""
    conn.execute(
        """
        INSERT INTO document_blobs (document_id, data, byte_size, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (document_id) DO UPDATE SET
            data = excluded.data, byte_size = excluded.byte_size, created_at = excluded.created_at
        """,
        (document_id, payload, len(payload), utcnow()),
    )


def read_blob(conn: sqlite3.Connection, document_id: int) -> bytes | None:
    row = conn.execute(
        "SELECT data FROM document_blobs WHERE document_id = ?", (document_id,)
    ).fetchone()
    return bytes(row["data"]) if row is not None else None


def read_bytes(settings: Settings, conn: sqlite3.Connection, document_id: int) -> bytes | None:
    """A document's bytes for serving: the durable blob if this document has
    been synced since document_blobs was introduced, else whatever's
    currently on local disk. The disk fallback exists only so documents
    synced before this table existed keep working until they're backfilled
    (see the Document Library's "Backfill" action) or naturally re-synced --
    disk is never the durable answer, just what's left of the old behavior."""
    blob = read_blob(conn, document_id)
    if blob is not None:
        return blob
    row = conn.execute("SELECT stored_path FROM documents WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        return None
    path = absolute_path(settings, row["stored_path"])
    return path.read_bytes() if path.exists() else None


def is_available(settings: Settings, conn: sqlite3.Connection, document_id: int) -> bool:
    """Cheap existence check for the GO Quality Scoring Engine's "PDF
    Availability" criterion -- same two-tier logic as read_bytes() (durable
    blob first, local disk fallback) but never reads the payload into
    memory, since a department health-table pass may check many documents.

    Callers checking MANY documents in a loop should use is_available_bulk()
    instead -- one call to this function is one DB round trip, and hundreds
    of them in a per-record loop is fine against a local SQLite file but
    turns into many real seconds against a remote connection (e.g. Turso in
    production) once there are hundreds of records to check."""
    has_blob = conn.execute(
        "SELECT 1 FROM document_blobs WHERE document_id = ?", (document_id,)
    ).fetchone() is not None
    if has_blob:
        return True
    row = conn.execute("SELECT stored_path FROM documents WHERE id = ?", (document_id,)).fetchone()
    return row is not None and absolute_path(settings, row["stored_path"]).exists()


def is_available_bulk(
    settings: Settings, conn: sqlite3.Connection, document_ids: list[int]
) -> dict[int, bool]:
    """Batched is_available(): 1-2 queries total instead of 1-2 per document.
    Same two-tier logic (durable blob first, local disk fallback) -- a
    document with a durable blob never needs the second query at all, which
    covers most production documents (Render's disk doesn't survive a
    redeploy, so document_blobs is the primary store there)."""
    unique_ids = list(dict.fromkeys(document_ids))
    if not unique_ids:
        return {}

    placeholders = ",".join("?" * len(unique_ids))
    with_blob = {
        int(r["document_id"])
        for r in conn.execute(
            f"SELECT document_id FROM document_blobs WHERE document_id IN ({placeholders})", unique_ids
        ).fetchall()
    }
    result: dict[int, bool] = {doc_id: True for doc_id in with_blob}

    remaining = [doc_id for doc_id in unique_ids if doc_id not in with_blob]
    if remaining:
        placeholders = ",".join("?" * len(remaining))
        stored_paths = {
            int(r["id"]): r["stored_path"]
            for r in conn.execute(
                f"SELECT id, stored_path FROM documents WHERE id IN ({placeholders})", remaining
            ).fetchall()
        }
        for doc_id in remaining:
            stored_path = stored_paths.get(doc_id)
            result[doc_id] = stored_path is not None and absolute_path(settings, stored_path).exists()

    return result


def ensure_file_on_disk(settings: Settings, conn: sqlite3.Connection, document_id: int) -> Path:
    """Guarantees the document PDF file exists on local disk at its expected
    repository_dir path. If the local file is missing (e.g. after a Render
    redeploy or reset), fetches the durable bytes from document_blobs / read_bytes
    and writes them to local disk so text/OCR extraction tools can read it."""
    row = conn.execute("SELECT stored_path FROM documents WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        raise LookupError(f"no document with id {document_id}")
    path = absolute_path(settings, row["stored_path"])
    if not path.exists():
        bytes_data = read_bytes(settings, conn, document_id)
        if bytes_data is None:
            raise RepositoryError(f"document {document_id} bytes not found in database or disk")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes_data)
    return path


def backfill_blobs_from_disk(settings: Settings, conn: sqlite3.Connection) -> dict:
    """One-time (repeatable) sweep: for every document with no durable blob
    yet, copy its bytes out of local disk and into document_blobs while
    they're still there. Safe to run any number of times -- store_blob()
    upserts, and a document already backfilled is skipped without touching
    disk again."""
    rows = conn.execute(
        """
        SELECT d.id, d.stored_path FROM documents d
         WHERE NOT EXISTS (SELECT 1 FROM document_blobs b WHERE b.document_id = d.id)
         ORDER BY d.id
        """
    ).fetchall()
    backfilled: list[int] = []
    missing: list[int] = []
    for row in rows:
        document_id = int(row["id"])
        path = absolute_path(settings, row["stored_path"])
        if not path.exists():
            missing.append(document_id)
            continue
        store_blob(conn, document_id, path.read_bytes())
        backfilled.append(document_id)
    return {"backfilled": backfilled, "missing_on_disk": missing}


def verify_document(
    settings: Settings, conn: sqlite3.Connection, document_id: int
) -> tuple[bool, str]:
    """Re-hash an archived document's bytes and compare to the recorded
    fingerprint. Checks the durable Turso blob first; only falls back to
    local disk for documents synced before document_blobs existed (see
    read_bytes())."""
    row = conn.execute(
        "SELECT stored_path, sha256 FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None:
        return False, "document not found"
    payload = read_bytes(settings, conn, document_id)
    if payload is None:
        return False, f"file missing from repository: {row['stored_path']}"
    actual = sha256_bytes(payload)
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


def _documents_where(
    *,
    source_id: int | None,
    search: str | None,
    department: str | None,
    year: int | None,
    language: str | None,
    status: str | None,
) -> tuple[str, list[object]]:
    """Shared by list_documents/count_documents so the two can never drift
    out of sync -- a filter that narrows the list but not the count (or vice
    versa) would silently break pagination."""
    clauses: list[str] = []
    params: list[object] = []
    if source_id is not None:
        clauses.append("d.source_id = ?")
        params.append(source_id)
    if search:
        clauses.append("(d.file_name LIKE ? OR go_number.value LIKE ?)")
        needle = f"%{search}%"
        params.extend([needle, needle])
    if department:
        clauses.append("s.department = ?")
        params.append(department)
    if year is not None:
        clauses.append("CAST(strftime('%Y', d.downloaded_at) AS INTEGER) = ?")
        params.append(year)
    if language:
        clauses.append("dc.language = ?")
        params.append(language)
    if status:
        clauses.append("COALESCE(r.status, dd.status) = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def list_documents(
    conn: sqlite3.Connection,
    *,
    source_id: int | None = None,
    search: str | None = None,
    department: str | None = None,
    year: int | None = None,
    language: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """One page of downloaded files, newest first, with enough context
    (source, department, parse/review status, GO number if extracted) for an
    admin to find and download a specific document without a command line.
    Paginated (see count_documents for the total) -- a library with hundreds
    of documents silently truncated at a fixed limit, with no way to reach
    the rest, is exactly the bug this replaces.

    `year` filters on the download date, not a parsed GO date -- GO dates
    live as free-text evidence in `go_fields` in whatever format the source
    document used, not a queryable column, so downloaded-year is the
    reliable one. `status` matches either the review outcome (approved/
    rejected/pending) or, for anything not yet reviewed, the discovery
    lifecycle stage (downloaded/parsed/etc) -- whichever the record has."""
    where, params = _documents_where(
        source_id=source_id, search=search, department=department,
        year=year, language=language, status=status,
    )
    params = [*params, limit, offset]
    return conn.execute(
        f"""
        SELECT
            d.id AS document_id,
            d.file_name,
            d.byte_size,
            d.downloaded_at,
            d.source_url,
            d.version,
            s.id AS source_id,
            s.name AS source_name,
            s.department AS source_department,
            dd.status AS discovery_status,
            r.id AS record_id,
            r.status AS review_status,
            go_number.value AS go_number,
            dc.language AS language
          FROM documents d
          JOIN sources s ON s.id = d.source_id
          LEFT JOIN discovered_documents dd ON dd.id = d.discovered_id
          LEFT JOIN extractions e ON e.document_id = d.id
          LEFT JOIN go_records r ON r.extraction_id = e.id
          LEFT JOIN go_fields go_number
                 ON go_number.record_id = r.id
                AND go_number.field_name = 'go_number'
                AND go_number.superseded_by IS NULL
          LEFT JOIN document_categories dc ON dc.document_id = d.id
         {where}
         ORDER BY d.downloaded_at DESC
         LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()


def count_documents(
    conn: sqlite3.Connection,
    *,
    source_id: int | None = None,
    search: str | None = None,
    department: str | None = None,
    year: int | None = None,
    language: str | None = None,
    status: str | None = None,
) -> int:
    """Total documents matching the same filters as list_documents, for
    computing page counts."""
    where, params = _documents_where(
        source_id=source_id, search=search, department=department,
        year=year, language=language, status=status,
    )
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n
          FROM documents d
          JOIN sources s ON s.id = d.source_id
          LEFT JOIN discovered_documents dd ON dd.id = d.discovered_id
          LEFT JOIN extractions e ON e.document_id = d.id
          LEFT JOIN go_records r ON r.extraction_id = e.id
          LEFT JOIN go_fields go_number
                 ON go_number.record_id = r.id
                AND go_number.field_name = 'go_number'
                AND go_number.superseded_by IS NULL
          LEFT JOIN document_categories dc ON dc.document_id = d.id
         {where}
        """,
        params,
    ).fetchone()
    return int(row["n"])


def list_document_departments(conn: sqlite3.Connection) -> list[str]:
    """Distinct departments that actually have a downloaded document, for
    the Document Library's Department filter dropdown."""
    return [
        r["department"] for r in conn.execute(
            "SELECT DISTINCT s.department FROM documents d JOIN sources s ON s.id = d.source_id ORDER BY 1"
        ).fetchall()
    ]


def list_document_years(conn: sqlite3.Connection) -> list[int]:
    """Distinct years documents were downloaded in, newest first."""
    return [
        int(r["y"]) for r in conn.execute(
            "SELECT DISTINCT CAST(strftime('%Y', downloaded_at) AS INTEGER) AS y "
            "FROM documents ORDER BY y DESC"
        ).fetchall()
    ]


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
