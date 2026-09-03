"""goengine/repository.py -- is_available_bulk() must behave identically to
calling is_available() once per document, just batched. Written after a real
production incident: department_health()/department_readiness() called
is_available() once per go_record with no batching, invisible at demo scale
(a handful of records) but many seconds of sequential DB round trips at real
production scale (hundreds of records), which read to the user as
/ops/quality and /ops/certification "circling" forever.
"""

from __future__ import annotations

from goengine import repository
from goengine.pipeline import run_all


def test_is_available_bulk_matches_is_available_per_document(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    document_ids = [int(r["id"]) for r in conn.execute("SELECT id FROM documents ORDER BY id").fetchall()]
    assert len(document_ids) >= 2

    expected = {doc_id: repository.is_available(settings, conn, doc_id) for doc_id in document_ids}
    actual = repository.is_available_bulk(settings, conn, document_ids)
    assert actual == expected
    assert all(expected.values())  # sampledata's documents are all really on disk


def test_is_available_bulk_false_for_a_document_missing_from_disk(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    row = conn.execute("SELECT id, stored_path FROM documents ORDER BY id LIMIT 1").fetchone()
    repository.absolute_path(settings, row["stored_path"]).unlink()

    result = repository.is_available_bulk(settings, conn, [row["id"]])
    assert result[row["id"]] is False


def test_is_available_bulk_true_via_durable_blob_even_if_disk_file_is_missing(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    row = conn.execute("SELECT id, stored_path FROM documents ORDER BY id LIMIT 1").fetchone()
    repository.store_blob(conn, row["id"], repository.absolute_path(settings, row["stored_path"]).read_bytes())
    repository.absolute_path(settings, row["stored_path"]).unlink()

    result = repository.is_available_bulk(settings, conn, [row["id"]])
    assert result[row["id"]] is True


def test_is_available_bulk_handles_duplicate_ids_and_empty_list(conn, settings, fetcher, source_id):
    run_all(conn, settings, fetcher, only_due=False)
    document_id = int(conn.execute("SELECT id FROM documents ORDER BY id LIMIT 1").fetchone()["id"])

    assert repository.is_available_bulk(settings, conn, []) == {}
    result = repository.is_available_bulk(settings, conn, [document_id, document_id])
    assert result == {document_id: True}


def test_is_available_bulk_uses_one_query_per_tier_not_one_per_document(conn, settings, fetcher, source_id):
    """The actual regression guard: query count must stay flat (~2 queries)
    regardless of how many documents are checked, not grow linearly."""
    run_all(conn, settings, fetcher, only_due=False)
    document_ids = [int(r["id"]) for r in conn.execute("SELECT id FROM documents ORDER BY id").fetchall()]
    assert len(document_ids) >= 2

    queries = []
    conn.set_trace_callback(lambda sql: queries.append(sql))
    try:
        repository.is_available_bulk(settings, conn, document_ids)
    finally:
        conn.set_trace_callback(None)

    assert len(queries) <= 2, f"expected at most 2 queries for {len(document_ids)} documents, got {len(queries)}"
