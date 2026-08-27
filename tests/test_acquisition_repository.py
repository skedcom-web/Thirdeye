"""Modules 3 & 4 -- acquisition and the write-once repository."""

from __future__ import annotations

import sqlite3

import pytest

from goengine import acquisition, registry, repository
from goengine.discovery import crawler
from goengine.pipeline import run_discovery, run_downloads


def _download_all(conn, settings, fetcher):
    run_discovery(conn, fetcher, only_due=False)
    return run_downloads(conn, settings, fetcher)


def test_download_archives_originals_with_fingerprints(conn, settings, fetcher, source_id, sample_pdfs):
    report = _download_all(conn, settings, fetcher)
    assert report.succeeded == len(sample_pdfs)

    rows = conn.execute("SELECT * FROM documents ORDER BY id").fetchall()
    assert len(rows) == len(sample_pdfs)

    for row in rows:
        stored = repository.absolute_path(settings, row["stored_path"])
        assert stored.exists()
        # The archived bytes are the served bytes, unchanged.
        assert repository.sha256_file(stored) == row["sha256"]
        assert row["byte_size"] == stored.stat().st_size
        assert row["source_url"].startswith("https://cms.tn.gov.in/")
        assert row["downloaded_at"]


def test_downloaded_bytes_are_identical_to_source(conn, settings, fetcher, source_id, sample_pdfs):
    _download_all(conn, settings, fetcher)
    original = {path.name: path.read_bytes() for _, path in sample_pdfs}

    for row in conn.execute("SELECT stored_path, source_url FROM documents").fetchall():
        name = row["source_url"].rsplit("/", 1)[-1]
        assert repository.absolute_path(settings, row["stored_path"]).read_bytes() == original[name]


def test_list_documents_paginates_without_gaps_or_overlap(conn, settings, fetcher, source_id, sample_pdfs):
    """Regression test for the Document Library silently truncating at a
    fixed limit with no way to reach the rest: count_documents() must agree
    with list_documents(), and consecutive pages must partition the full
    set with no duplicate or missing rows."""
    _download_all(conn, settings, fetcher)
    total = repository.count_documents(conn)
    assert total == len(sample_pdfs)

    page1 = repository.list_documents(conn, limit=2, offset=0)
    page2 = repository.list_documents(conn, limit=2, offset=2)
    assert len(page1) == 2
    assert len(page1) + len(page2) == total
    ids_page1 = {d["document_id"] for d in page1}
    ids_page2 = {d["document_id"] for d in page2}
    assert ids_page1.isdisjoint(ids_page2)


def test_count_documents_respects_filters(conn, settings, fetcher, source_id, sample_pdfs):
    _download_all(conn, settings, fetcher)
    assert repository.count_documents(conn, source_id=source_id) == len(sample_pdfs)
    assert repository.count_documents(conn, source_id=source_id + 1000) == 0


def test_documents_cannot_be_overwritten(conn, settings, fetcher, source_id):
    _download_all(conn, settings, fetcher)
    document_id = conn.execute("SELECT MIN(id) AS id FROM documents").fetchone()["id"]

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE documents SET sha256 = 'tampered' WHERE id = ?", (document_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))


def test_changed_source_document_creates_a_new_version(conn, settings, fetcher, source_id, sample_pdfs):
    _download_all(conn, settings, fetcher)

    first = conn.execute("SELECT * FROM documents ORDER BY id LIMIT 1").fetchone()
    discovered_id = int(first["discovered_id"])
    url = conn.execute(
        "SELECT url FROM discovered_documents WHERE id = ?", (discovered_id,)
    ).fetchone()["url"]

    # The government republishes a corrected PDF at the same URL.
    revised = sample_pdfs[0][1].read_bytes() + b"\n% revised\n"
    fetcher.add_bytes(url, revised)
    result = acquisition.redownload(conn, settings, fetcher, discovered_id)

    assert result.ok and result.new_version
    history = repository.version_history(conn, discovered_id)
    assert [row["version"] for row in history] == [1, 2]
    assert history[1]["supersedes_id"] == history[0]["id"]

    # The superseded original is still on disk, untouched.
    assert repository.absolute_path(settings, history[0]["stored_path"]).exists()
    assert history[0]["sha256"] != history[1]["sha256"]


def test_redownload_of_unchanged_file_makes_no_new_version(conn, settings, fetcher, source_id):
    _download_all(conn, settings, fetcher)
    first = conn.execute("SELECT * FROM documents ORDER BY id LIMIT 1").fetchone()
    discovered_id = int(first["discovered_id"])

    result = acquisition.redownload(conn, settings, fetcher, discovered_id)
    assert result.ok and not result.new_version
    assert len(repository.version_history(conn, discovered_id)) == 1


def test_identical_bytes_are_stored_once(settings):
    payload = b"%PDF-1.4\nidentical\n"
    first = repository.store(settings, payload)
    second = repository.store(settings, payload)

    assert first.sha256 == second.sha256
    assert second.deduplicated is True
    assert first.absolute_path == second.absolute_path


def test_non_pdf_response_is_rejected(conn, settings, fetcher, source_id):
    run_discovery(conn, fetcher, only_due=False)
    row = conn.execute("SELECT * FROM discovered_documents ORDER BY id LIMIT 1").fetchone()
    fetcher.add_bytes(row["url"], b"<html>404 not found</html>", content_type="text/html")

    result = acquisition.acquire_one(conn, settings, fetcher, int(row["id"]))

    assert not result.ok
    assert "not a PDF" in result.error
    status = conn.execute(
        "SELECT status FROM discovered_documents WHERE id = ?", (row["id"],)
    ).fetchone()["status"]
    assert status == crawler.STATUS_REJECTED


def test_download_refuses_url_outside_the_allowlist(conn, settings, fetcher, source_id):
    from goengine.db import utcnow

    cur = conn.execute(
        """
        INSERT INTO discovered_documents
            (source_id, url, link_text, found_on_url, discovered_at, last_seen_at, status)
        VALUES (?, 'https://news.example.com/go.pdf', '', '', ?, ?, 'new')
        """,
        (source_id, utcnow(), utcnow()),
    )
    result = acquisition.acquire_one(conn, settings, fetcher, int(cur.lastrowid))

    assert not result.ok
    assert "not an approved government source" in result.error


def test_integrity_check_detects_a_tampered_file(conn, settings, fetcher, source_id):
    _download_all(conn, settings, fetcher)
    row = conn.execute("SELECT id, stored_path FROM documents LIMIT 1").fetchone()

    repository.absolute_path(settings, row["stored_path"]).write_bytes(b"%PDF-1.4 tampered")
    ok, message = repository.verify_document(settings, conn, int(row["id"]))

    assert not ok
    assert "hash mismatch" in message


def test_file_name_derivation():
    assert acquisition.derive_file_name(
        "https://cms.tn.gov.in/x/abc.pdf", "G.O. (Ms) No. 123, dated 2026"
    ) == "GO-123-2026.pdf"
    assert acquisition.derive_file_name("https://cms.tn.gov.in/x/order_55.pdf") == "order_55.pdf"
