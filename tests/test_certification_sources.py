"""Module 1 -- Source Certification Engine."""

from __future__ import annotations

from goengine import registry
from goengine.certification.sources import (
    RESULT_CERTIFIED,
    RESULT_FAILED,
    RESULT_PARTIAL,
    certification_history,
    certification_summary,
    certify_all,
    certify_source,
)
from goengine.fetching import OfflineFetcher


def test_healthy_source_is_fully_certified(conn, settings, fetcher, source_id):
    source = registry.get_source(conn, source_id)
    result = certify_source(conn, settings, fetcher, source.id)

    assert result.result == RESULT_CERTIFIED
    assert result.connectivity_ok
    assert result.discovery_ok
    assert result.download_ok
    assert result.stability_ok
    assert result.authenticity_ok
    assert result.documents_discovered == 3
    assert result.documents_downloaded == 3


def test_certification_updates_source_status_and_timestamps(conn, settings, fetcher, source_id):
    certify_source(conn, settings, fetcher, source_id)
    row = conn.execute(
        "SELECT certification_status, certification_date, last_crawl_success_at "
        "FROM sources WHERE id = ?",
        (source_id,),
    ).fetchone()
    assert row["certification_status"] == RESULT_CERTIFIED
    assert row["certification_date"]
    assert row["last_crawl_success_at"]


def test_unreachable_source_fails(conn, settings, source_id):
    empty = OfflineFetcher()  # nothing registered -> every fetch raises
    result = certify_source(conn, settings, empty, source_id)

    assert result.result == RESULT_FAILED
    assert not result.connectivity_ok
    assert result.discovery_ok is False
    assert "unreachable" in result.messages["connectivity"]


def test_reachable_but_undownloadable_source_is_partial(conn, settings, source_id):
    offline = OfflineFetcher()
    offline.add_html(
        "https://cms.tn.gov.in/go-search",
        '<a href="https://cms.tn.gov.in/go/GO-1-2026.pdf">G.O. Ms No. 1</a>',
    )
    offline.add_bytes(
        "https://cms.tn.gov.in/go/GO-1-2026.pdf", b"<html>not a pdf</html>", content_type="text/html"
    )
    result = certify_source(conn, settings, offline, source_id)

    assert result.result == RESULT_PARTIAL
    assert result.connectivity_ok
    assert result.discovery_ok
    assert not result.download_ok


def test_host_no_longer_approved_fails_authenticity(conn, settings, fetcher):
    # Register with an approved host, then simulate policy drift by editing
    # the row directly (assert_approved is re-checked live, not cached).
    source_id = registry.add_source(
        conn, name="Some Portal", department="X", url="https://cms.tn.gov.in/x",
        source_type="go_portal",
    )
    conn.execute("UPDATE sources SET url = 'https://evil.example.com/x' WHERE id = ?", (source_id,))
    row = registry.get_source(conn, source_id)
    result = certify_source(conn, settings, fetcher, source_id)

    assert not result.authenticity_ok
    assert result.result == RESULT_FAILED


def test_certification_history_is_append_only_across_runs(conn, settings, fetcher, source_id):
    certify_source(conn, settings, fetcher, source_id)
    certify_source(conn, settings, fetcher, source_id)
    history = certification_history(conn, source_id)
    assert len(history) == 2


def test_certify_all_covers_every_active_source(conn, settings, fetcher, source_id):
    registry.add_source(
        conn, name="Inactive Source", department="X", url="https://cms.tn.gov.in/y",
        source_type="go_portal", active=False,
    )
    results = certify_all(conn, settings, fetcher)
    assert [r.source_id for r in results] == [source_id]


def test_certification_summary_counts_by_status(conn, settings, fetcher, source_id):
    certify_source(conn, settings, fetcher, source_id)
    summary = certification_summary(conn)
    assert summary[RESULT_CERTIFIED] == 1
    assert summary["PENDING"] == 0


def test_certify_source_writes_audit_entry(conn, settings, fetcher, source_id):
    from goengine import audit

    certify_source(conn, settings, fetcher, source_id)
    entries = audit.trail(conn, entity_type="source", entity_id=source_id)
    assert any(e.action == "source.certification_run" for e in entries)
