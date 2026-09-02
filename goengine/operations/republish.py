"""Phase 3.9 Initiatives 6 & 7 -- Published GO Edit & Republish Workflow.

Workflow: Published GO -> Edit Requested (REVISION_DRAFT) -> Review
(REVISION_PENDING_REVIEW) -> Republish (REPUBLISHED, applied) | Reject
(REJECTED, discarded). go_records.status stays 'approved' throughout -- the
citizen-facing record never disappears or shows blank data mid-review; only
the moment of approval flips live field values.

Only subject/budget/district/scheme_name are ever editable here -- NOT
go_number/go_date/department, the three fields go_identity.py's
compute_identity() uses to derive canonical_go_id/go_url_slug. That function
unconditionally overwrites the slug whenever those fields change, with no
preservation of an already-assigned one, so allowing them through this
workflow could silently break a citizen's bookmarked permanent URL --
exactly what the blueprint's "Permanent URLs remain unchanged" prohibits.
This is enforced here (request_revision rejects any other field name) and
reused as a second layer of defense via review.correct_field() itself,
which is the only function that ever writes the actual field change.
"""

from __future__ import annotations

import json
import sqlite3

from .. import audit, repository, review
from ..config import Settings
from ..db import utcnow

EDITABLE_FIELDS = ("subject", "budget", "district", "scheme_name")

STATUS_DRAFT = "REVISION_DRAFT"
STATUS_PENDING_REVIEW = "REVISION_PENDING_REVIEW"
STATUS_REPUBLISHED = "REPUBLISHED"
STATUS_REJECTED = "REJECTED"


class RepublishError(ValueError):
    pass


def _current_value(conn: sqlite3.Connection, record_id: int, field_name: str) -> str | None:
    row = conn.execute(
        "SELECT normalized_value FROM go_fields WHERE record_id = ? AND field_name = ? AND superseded_by IS NULL",
        (record_id, field_name),
    ).fetchone()
    return row["normalized_value"] if row is not None else None


def _get_revision(conn: sqlite3.Connection, revision_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM go_record_revisions WHERE id = ?", (revision_id,)).fetchone()
    if row is None:
        raise LookupError(f"no revision with id {revision_id}")
    return row


def request_revision(
    conn: sqlite3.Connection, record_id: int, *, editor: str, changes: dict[str, str], reason: str
) -> int:
    """Opens a REVISION_DRAFT for an already-published record. Nothing here
    touches live data -- that only happens in approve_revision()."""
    record = conn.execute("SELECT * FROM go_records WHERE id = ?", (record_id,)).fetchone()
    if record is None:
        raise LookupError(f"no GO record with id {record_id}")
    if record["status"] != review.STATUS_APPROVED:
        raise RepublishError("only a published (approved) record can be revised")
    if not editor:
        raise RepublishError("an editor identity is required")
    if not reason:
        raise RepublishError("a reason is required to request a revision")
    if not changes:
        raise RepublishError("at least one field change is required")

    for field_name in changes:
        if field_name not in EDITABLE_FIELDS:
            raise RepublishError(
                f"{field_name!r} cannot be changed via republish -- only {EDITABLE_FIELDS} are editable; "
                "GO number, GO date, and department are frozen once published to protect permanent URLs"
            )

    change_list = [
        {"field_name": name, "old_value": _current_value(conn, record_id, name), "new_value": new_value}
        for name, new_value in changes.items()
    ]

    version = int(record["current_version"]) + 1
    now = utcnow()
    cur = conn.execute(
        """
        INSERT INTO go_record_revisions
            (record_id, version, status, requested_by, requested_at, reason, changes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (record_id, version, STATUS_DRAFT, editor, now, reason, json.dumps(change_list)),
    )
    revision_id = int(cur.lastrowid)
    audit.record(
        conn, action="revision.requested", entity_type="go_record", entity_id=record_id,
        actor=editor, detail={"revision_id": revision_id, "version": version, "changes": change_list, "reason": reason},
    )
    return revision_id


def submit_for_review(conn: sqlite3.Connection, revision_id: int, *, editor: str) -> None:
    revision = _get_revision(conn, revision_id)
    if revision["status"] != STATUS_DRAFT:
        raise RepublishError(f"revision {revision_id} is {revision['status']}, not a draft")
    conn.execute(
        "UPDATE go_record_revisions SET status = ? WHERE id = ?", (STATUS_PENDING_REVIEW, revision_id)
    )
    audit.record(
        conn, action="revision.submitted", entity_type="go_record", entity_id=int(revision["record_id"]),
        actor=editor, detail={"revision_id": revision_id},
    )


def approve_revision(conn: sqlite3.Connection, revision_id: int, settings: Settings, *, reviewer: str) -> None:
    """Applies the revision's changes via the existing review.correct_field()
    -- reusing its go_fields history/audit trail rather than duplicating it
    -- then bumps go_records.current_version. Never calls go_identity's
    compute_identity() (correct_field() only does that for go_number/go_date,
    which this workflow never touches), so the permanent URL is provably
    unchanged by this function, not just by convention."""
    revision = _get_revision(conn, revision_id)
    if revision["status"] not in (STATUS_DRAFT, STATUS_PENDING_REVIEW):
        raise RepublishError(f"revision {revision_id} is {revision['status']}, not pending")
    if not reviewer:
        raise RepublishError("a reviewer identity is required to approve")
    if reviewer == revision["requested_by"]:
        raise RepublishError("the requester cannot approve their own revision -- review approval is required")

    record_id = int(revision["record_id"])
    record = conn.execute(
        """
        SELECT r.*, s.department AS department
          FROM go_records r JOIN sources s ON s.id = r.source_id
         WHERE r.id = ?
        """,
        (record_id,),
    ).fetchone()
    if record is None:
        raise LookupError(f"no GO record with id {record_id}")

    # Guardrails (Initiative 7): re-checked at approval time, not just when
    # the revision was first requested -- source availability in particular
    # can change in between. go_number_numeric/go_year (not the raw text
    # column) are only ever non-null when go_identity.py's parsing actually
    # succeeded, so this checks "present and valid", not just "present".
    if record["go_number_numeric"] is None:
        raise RepublishError("cannot republish without a GO number")
    if record["go_year"] is None:
        raise RepublishError("cannot republish without a GO date")
    if not record["department"]:
        raise RepublishError("cannot republish without a department")
    if not repository.is_available(settings, conn, int(record["document_id"])):
        raise RepublishError("cannot republish without the source PDF")

    changes = json.loads(revision["changes"])
    for change in changes:
        field_name = change["field_name"]
        if field_name not in EDITABLE_FIELDS:
            raise RepublishError(f"{field_name!r} is not editable via republish")
        review.correct_field(
            conn, record_id, field_name, change["new_value"],
            reviewer=reviewer, note=f"Republish: {revision['reason']}",
        )

    now = utcnow()
    conn.execute("UPDATE go_records SET current_version = ? WHERE id = ?", (int(revision["version"]), record_id))
    conn.execute(
        """
        UPDATE go_record_revisions
           SET status = ?, reviewed_by = ?, reviewed_at = ?, republished_at = ?
         WHERE id = ?
        """,
        (STATUS_REPUBLISHED, reviewer, now, now, revision_id),
    )
    audit.record(
        conn, action="record.republished", entity_type="go_record", entity_id=record_id,
        actor=reviewer, detail={"revision_id": revision_id, "version": int(revision["version"]), "changes": changes},
    )


def reject_revision(conn: sqlite3.Connection, revision_id: int, *, reviewer: str, reason: str) -> None:
    revision = _get_revision(conn, revision_id)
    if revision["status"] not in (STATUS_DRAFT, STATUS_PENDING_REVIEW):
        raise RepublishError(f"revision {revision_id} is {revision['status']}, not pending")
    if not reviewer:
        raise RepublishError("a reviewer identity is required to reject")
    if not reason:
        raise RepublishError("a reason is required to reject a revision")

    conn.execute(
        """
        UPDATE go_record_revisions
           SET status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ?
         WHERE id = ?
        """,
        (STATUS_REJECTED, reviewer, utcnow(), reason, revision_id),
    )
    audit.record(
        conn, action="revision.rejected", entity_type="go_record", entity_id=int(revision["record_id"]),
        actor=reviewer, detail={"revision_id": revision_id, "reason": reason},
    )


def revision_history(conn: sqlite3.Connection, record_id: int) -> list[dict]:
    """Full revision history for one record, newest first -- the admin view."""
    rows = conn.execute(
        "SELECT * FROM go_record_revisions WHERE record_id = ? ORDER BY version DESC", (record_id,)
    ).fetchall()
    return [
        {
            "id": int(r["id"]), "version": int(r["version"]), "status": r["status"],
            "requested_by": r["requested_by"], "requested_at": r["requested_at"], "reason": r["reason"],
            "changes": json.loads(r["changes"]), "reviewed_by": r["reviewed_by"],
            "reviewed_at": r["reviewed_at"], "review_note": r["review_note"], "republished_at": r["republished_at"],
        }
        for r in rows
    ]


def republish_status(conn: sqlite3.Connection, record_id: int) -> dict:
    """Citizen-safe summary: version number and date only -- no reviewer
    identity, no before/after values (those stay in the admin-only history)."""
    record = conn.execute("SELECT current_version FROM go_records WHERE id = ?", (record_id,)).fetchone()
    current_version = int(record["current_version"]) if record else 1
    last_republished_at = conn.execute(
        """
        SELECT republished_at FROM go_record_revisions
         WHERE record_id = ? AND status = ?
         ORDER BY version DESC LIMIT 1
        """,
        (record_id, STATUS_REPUBLISHED),
    ).fetchone()
    return {
        "current_version": current_version,
        "last_republished_at": last_republished_at["republished_at"] if last_republished_at else None,
        "has_history": current_version > 1,
    }
