"""JSON export of the review/extraction layer -- documents and document_blobs
(the archived PDF evidence) are intentionally excluded: they're permanent by
schema design (see repository.py, documents_no_delete trigger) and never at
risk from anything in this module, so backing them up here would just be
redundant weight. This exports what reset.reset_for_production() actually
removes: go_records, their fields, and review history -- available any time
an admin wants a point-in-time snapshot, not only right before a reset.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def export_review_snapshot(conn: sqlite3.Connection) -> dict:
    records = []
    for r in conn.execute(
        """
        SELECT r.id, r.status, r.reviewed_by, r.reviewed_at, r.review_note, r.created_at,
               r.extractor_version, d.file_name, d.source_url, d.sha256, s.name AS source_name,
               dc.department_bucket, dc.language
          FROM go_records r
          JOIN documents d ON d.id = r.document_id
          JOIN sources s ON s.id = r.source_id
          LEFT JOIN document_categories dc ON dc.document_id = r.document_id
         ORDER BY r.id
        """
    ).fetchall():
        fields = [
            dict(f)
            for f in conn.execute(
                """
                SELECT field_name, value, normalized_value, source_page, source_text,
                       confidence, method, origin, created_by, created_at
                  FROM go_fields WHERE record_id = ? AND superseded_by IS NULL
                 ORDER BY field_name
                """,
                (r["id"],),
            ).fetchall()
        ]
        records.append({**dict(r), "fields": fields})

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "records": records,
    }
