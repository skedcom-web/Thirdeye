"""Regression test for a real bug found live: pipeline.run_all(source_id=X)
only ever scoped the discovery/crawl phase to X -- the download and parse
phases pulled from the global pending queue regardless, so a run scoped to
one source would silently process another source's backlog instead. Caught
when a Local Extraction Agent request scoped to "Public Works" kept
downloading "Health" department leftovers."""

import sqlite3

from goengine import pipeline, registry
from goengine.fetching import OfflineFetcher


def _register_source(conn, name, department, listing_url):
    return registry.add_source(
        conn, name=name, department=department, url=listing_url,
        source_type="go_portal", adapter="generic_links", actor="test",
    )


def test_run_all_source_id_scopes_download_and_parse_phases(conn, settings, tmp_path):
    # Two sources, each with one discoverable PDF, sharing one fetcher.
    fetcher = OfflineFetcher()

    sid_a = _register_source(conn, "Source A", "Dept A", "https://tn.gov.in/a")
    sid_b = _register_source(conn, "Source B", "Dept B", "https://tn.gov.in/b")

    pdf_bytes = (tmp_path / "dummy.pdf")
    pdf_bytes.write_bytes(b"%PDF-1.4\n%useless placeholder for looks_like_pdf\n")
    payload = pdf_bytes.read_bytes()

    fetcher.add_html(
        "https://tn.gov.in/a",
        '<html><body><a href="https://tn.gov.in/a/doc.pdf">GO A</a></body></html>',
    )
    fetcher.add_bytes("https://tn.gov.in/a/doc.pdf", payload)
    fetcher.add_html(
        "https://tn.gov.in/b",
        '<html><body><a href="https://tn.gov.in/b/doc.pdf">GO B</a></body></html>',
    )
    fetcher.add_bytes("https://tn.gov.in/b/doc.pdf", payload)

    # Discover both sources' documents first (simulates them both already
    # being queued, e.g. from an earlier run), WITHOUT downloading either.
    pipeline.run_discovery(conn, fetcher, only_due=False, source_id=sid_a, max_pages=5)
    pipeline.run_discovery(conn, fetcher, only_due=False, source_id=sid_b, max_pages=5)

    discovered = conn.execute("SELECT source_id FROM discovered_documents").fetchall()
    assert {r["source_id"] for r in discovered} == {sid_a, sid_b}
    assert conn.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"] == 0

    # Now run the full pipeline scoped ONLY to source A.
    report = pipeline.run_all(conn, settings, fetcher, only_due=False, source_id=sid_a, max_pages=5, limit=50)

    downloaded = conn.execute("SELECT source_id FROM documents").fetchall()
    downloaded_source_ids = {r["source_id"] for r in downloaded}

    # The bug: this used to also download source B's pending document even
    # though only source A was requested.
    assert downloaded_source_ids == {sid_a}, (
        f"run_all(source_id={sid_a}) downloaded from unrelated source(s): {downloaded_source_ids - {sid_a}}"
    )
    assert report.download.succeeded == 1

    # Source B's document must still be sitting untouched, pending.
    b_status = conn.execute(
        "SELECT status FROM discovered_documents WHERE source_id = ?", (sid_b,)
    ).fetchone()["status"]
    assert b_status == "new"


def test_run_downloads_source_id_filter(conn, settings, tmp_path):
    fetcher = OfflineFetcher()
    sid_a = _register_source(conn, "Source A", "Dept A", "https://tn.gov.in/a2")
    sid_b = _register_source(conn, "Source B", "Dept B", "https://tn.gov.in/b2")

    payload = b"%PDF-1.4\nplaceholder\n"
    fetcher.add_html("https://tn.gov.in/a2", '<html><body><a href="https://tn.gov.in/a2/x.pdf">A</a></body></html>')
    fetcher.add_bytes("https://tn.gov.in/a2/x.pdf", payload)
    fetcher.add_html("https://tn.gov.in/b2", '<html><body><a href="https://tn.gov.in/b2/y.pdf">B</a></body></html>')
    fetcher.add_bytes("https://tn.gov.in/b2/y.pdf", payload)

    pipeline.run_discovery(conn, fetcher, only_due=False, source_id=sid_a, max_pages=5)
    pipeline.run_discovery(conn, fetcher, only_due=False, source_id=sid_b, max_pages=5)

    report = pipeline.run_downloads(conn, settings, fetcher, limit=50, source_id=sid_b)
    assert report.succeeded == 1

    rows = conn.execute("SELECT source_id FROM documents").fetchall()
    assert {r["source_id"] for r in rows} == {sid_b}
