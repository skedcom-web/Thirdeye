"""Pre-flight check for the Render -> Netlify cutover: confirms every
document's bytes already live in Turso's document_blobs table, not just on
Render's local disk. Once Render is decommissioned, that disk is gone --
any document without a durable blob row becomes permanently unrecoverable
through the app.

Read-only. Makes no writes, calls no backfill function -- it only reports.

Usage:
    TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... python scripts/check_blob_backfill.py

Requires the *production* Turso credentials (this repo/session has none),
so run it from wherever those are available -- e.g. as a one-off shell on
the Render instance itself, or from your own machine with the values from
the Render dashboard's environment settings.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    url = os.environ.get("TURSO_DATABASE_URL")
    token = os.environ.get("TURSO_AUTH_TOKEN")
    if not url or not token:
        print("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must both be set in the environment.")
        return 2

    from goengine.turso_db import TursoConnection

    conn = TursoConnection(url, token)
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        blobbed = conn.execute("SELECT COUNT(*) AS n FROM document_blobs").fetchone()["n"]
        missing_rows = conn.execute(
            """
            SELECT d.id, d.stored_path, d.source_id
              FROM documents d
             WHERE NOT EXISTS (SELECT 1 FROM document_blobs b WHERE b.document_id = d.id)
             ORDER BY d.id
            """
        ).fetchall()
        missing = len(missing_rows)

        print(f"documents:       {total}")
        print(f"document_blobs:  {blobbed}")
        print(f"missing backfill: {missing}")

        if missing:
            print()
            print("These documents have NO durable blob and only exist on Render's local")
            print("disk today. Run repository.backfill_blobs_from_disk() against THIS SAME")
            print("Render instance (its disk, not this checker) before decommissioning Render,")
            print("or they will become unrecoverable once that disk is gone:")
            for row in missing_rows[:50]:
                print(f"  document id={row['id']} source_id={row['source_id']} stored_path={row['stored_path']}")
            if missing > 50:
                print(f"  ... and {missing - 50} more")
            return 1

        print()
        print("All documents are backfilled. Safe to decommission Render's disk.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
