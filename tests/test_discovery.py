"""Module 2 -- discovery, deduplication and status lifecycle."""

from __future__ import annotations

import pytest

from goengine import audit
from goengine.discovery import crawler
from goengine.discovery.adapters import get_adapter
from goengine.registry import Source


def test_crawl_discovers_every_listed_document(conn, source_id, fetcher, sample_pdfs):
    from goengine import registry

    source = registry.get_source(conn, source_id)
    result = crawler.crawl_source(conn, fetcher, source)

    assert result.status == "ok"
    assert result.new_documents == len(sample_pdfs)
    assert crawler.counts_by_status(conn)["new"] == len(sample_pdfs)


def test_recrawling_does_not_duplicate(conn, source_id, fetcher, sample_pdfs):
    from goengine import registry

    source = registry.get_source(conn, source_id)
    crawler.crawl_source(conn, fetcher, source)
    second = crawler.crawl_source(conn, fetcher, registry.get_source(conn, source_id))

    assert second.new_documents == 0
    assert second.duplicate_documents == len(sample_pdfs)

    total = conn.execute("SELECT COUNT(*) AS n FROM discovered_documents").fetchone()["n"]
    assert total == len(sample_pdfs)


def test_discovery_writes_audit_entries(conn, source_id, fetcher):
    from goengine import registry

    crawler.crawl_source(conn, fetcher, registry.get_source(conn, source_id))
    actions = [e.action for e in audit.trail(conn, limit=100)]
    assert "crawl.started" in actions
    assert "document.discovered" in actions
    assert "crawl.finished" in actions


def test_status_transitions_are_audited(conn, source_id, fetcher):
    from goengine import registry

    crawler.crawl_source(conn, fetcher, registry.get_source(conn, source_id))
    discovered_id = conn.execute("SELECT MIN(id) AS id FROM discovered_documents").fetchone()["id"]

    crawler.set_status(conn, discovered_id, crawler.STATUS_DOWNLOADED)
    entry = audit.trail(conn, entity_type="discovered_document", entity_id=discovered_id)[0]
    assert entry.action == "document.status_changed"
    assert (entry.before_value, entry.after_value) == ("new", "downloaded")


def test_invalid_status_is_refused(conn, source_id, fetcher):
    from goengine import registry

    crawler.crawl_source(conn, fetcher, registry.get_source(conn, source_id))
    discovered_id = conn.execute("SELECT MIN(id) AS id FROM discovered_documents").fetchone()["id"]
    with pytest.raises(ValueError):
        crawler.set_status(conn, discovered_id, "published")


def test_crawl_failure_is_recorded_not_raised(conn, source_id):
    from goengine import registry
    from goengine.fetching import OfflineFetcher

    empty = OfflineFetcher()  # no responses registered -> FetchError
    result = crawler.crawl_source(conn, empty, registry.get_source(conn, source_id))

    assert result.status == "error"
    assert result.error
    assert registry.get_source(conn, source_id).last_crawl_status == "error"


def test_adapter_skips_offsite_links():
    adapter = get_adapter("generic_links")
    html = """
      <a href="https://cms.tn.gov.in/sites/go/GO-1-2026.pdf">G.O. Ms No. 1</a>
      <a href="https://news.example.com/go-1.pdf">news coverage</a>
      <a href="https://twitter.com/x/go.pdf">tweet</a>
    """
    page = adapter.parse(html, "https://cms.tn.gov.in/go-search")
    assert [link.url for link in page.documents] == [
        "https://cms.tn.gov.in/sites/go/GO-1-2026.pdf"
    ]


def test_adapter_resolves_relative_urls_and_drops_fragments():
    adapter = get_adapter("generic_links")
    html = '<a href="/sites/go/GO-9-2026.pdf#page=2">G.O. Ms No. 9</a>'
    page = adapter.parse(html, "https://cms.tn.gov.in/go-search")
    assert page.documents[0].url == "https://cms.tn.gov.in/sites/go/GO-9-2026.pdf"


def test_portal_adapter_captures_listing_hints(sample_pdfs):
    from tests.conftest import build_listing_html

    adapter = get_adapter("tn_go_portal")
    page = adapter.parse(build_listing_html(sample_pdfs), "https://cms.tn.gov.in/go-search")

    assert len(page.documents) == len(sample_pdfs)
    hints = page.documents[0].hints
    assert hints["go_number"] == sample_pdfs[0][0].go_number
    assert hints["department"] == sample_pdfs[0][0].department


def test_portal_adapter_follows_department_directory_pages():
    """A directory page (tn.gov.in/godept_list.php-shaped) has no PDFs of its
    own -- only links to each department's own listing page. Those must be
    queued for follow, or a crawl that starts on the directory dead-ends."""
    adapter = get_adapter("tn_go_portal")
    html = """
      <a href="go.php?dep_id=Mg==&year=MjAyNw==">Agriculture Department</a>
      <a href="go.php?dep_id=MTE=&year=MjAyNw==">Health and Family Welfare Department</a>
      <a href="https://cms.tn.gov.in/cms_migrated/document/GO/rd_e_ms_134_2026.pdf">Direct GO PDF</a>
      <a href="https://news.example.com/unrelated">off-site link</a>
      <a href="javascript:void(0)">no-op</a>
    """
    page = adapter.parse(html, "https://www.tn.gov.in/godept_list.php")

    assert [d.url for d in page.documents] == [
        "https://cms.tn.gov.in/cms_migrated/document/GO/rd_e_ms_134_2026.pdf"
    ]
    assert page.follow == [
        "https://www.tn.gov.in/go.php?dep_id=Mg==&year=MjAyNw==",
        "https://www.tn.gov.in/go.php?dep_id=MTE=&year=MjAyNw==",
    ]


def test_portal_adapter_parses_a_real_shaped_department_table():
    """Shaped after the live tn.gov.in/go.php department listing table."""
    adapter = get_adapter("tn_go_portal")
    html = """
      <table>
        <tr><th>Date</th><th>G.O. Number</th><th>Subject</th></tr>
        <tr>
          <td>31-07-2026</td>
          <td><a href="https://cms.tn.gov.in/cms_migrated/document/GO/health_ms_221_2026.pdf">G.O.(MS)No.221</a></td>
          <td>Implementation of Advance Medical Directives - Guidelines - Issued.</td>
        </tr>
      </table>
    """
    page = adapter.parse(html, "https://www.tn.gov.in/go.php?dep_id=MTE=&year=MjAyNg==")

    assert len(page.documents) == 1
    doc = page.documents[0]
    assert doc.url == "https://cms.tn.gov.in/cms_migrated/document/GO/health_ms_221_2026.pdf"
    assert doc.hints["go_date"] == "31-07-2026"
    assert doc.hints["go_number"] == "G.O.(MS)No.221"


def test_is_due_respects_frequency():
    def make(frequency: str, last: str | None, active: bool = True) -> Source:
        return Source(
            id=1, name="s", department="d", url="https://tn.gov.in", host="tn.gov.in",
            source_type="go_portal", adapter="generic_links", active=active,
            crawl_frequency=frequency, last_crawl_at=last, last_crawl_status=None, notes=None,
            priority="Medium", source_category=None,
        )

    assert crawler.is_due(make("daily", None)) is True
    assert crawler.is_due(make("daily", "2020-01-01T00:00:00+00:00")) is True
    assert crawler.is_due(make("manual", None)) is False
    assert crawler.is_due(make("daily", None, active=False)) is False
