"""Module 1 -- the trust boundary. These are the governance tests."""

from __future__ import annotations

import pytest

from goengine import audit, registry


@pytest.mark.parametrize(
    "url",
    [
        "https://cms.tn.gov.in/go-search",
        "https://stationeryprinting.tn.gov.in/gazette.php",
        "https://tn.gov.in/",
        "https://tnega.org/orders",
    ],
)
def test_government_hosts_are_approved(url):
    assert registry.is_approved(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://timesofindia.indiatimes.com/go-123",   # news
        "https://twitter.com/CMOTamilNadu/status/1",     # social media
        "https://someblog.wordpress.com/tn-go",          # blog
        "https://tn-gov-in.example.com/go",              # lookalike domain
        "https://evil.com/tn.gov.in/go.pdf",             # path, not host
        "ftp://cms.tn.gov.in/go.pdf",                    # non-http scheme
    ],
)
def test_unofficial_sources_are_rejected(url):
    assert not registry.is_approved(url)
    with pytest.raises(registry.SourceRejected):
        registry.assert_approved(url)


def test_blocked_hosts_rejected_despite_gov_suffix():
    # pib.gov.in is a government host but publishes press releases, not orders.
    assert not registry.is_approved("https://pib.gov.in/PressRelease.aspx")


def test_add_source_rejects_unofficial_url(conn):
    with pytest.raises(registry.SourceRejected):
        registry.add_source(
            conn,
            name="Some News Site",
            department="News",
            url="https://news.example.com/tn-orders",
            source_type="department_site",
        )
    assert registry.list_sources(conn) == []


def test_add_source_writes_audit_entry(conn, source_id):
    entries = audit.trail(conn, entity_type="source", entity_id=source_id)
    assert [e.action for e in entries] == ["source.registered"]
    assert entries[0].after_value == "https://cms.tn.gov.in/go-search"


def test_seed_is_idempotent(conn):
    first = registry.seed(conn)
    second = registry.seed(conn)
    assert first == second
    assert len(registry.list_sources(conn)) == len(first)


def test_seed_department_urls_carry_the_current_year_and_the_tn_adapter(conn):
    import base64
    from datetime import datetime, timezone

    registry.seed(conn)
    expected_year = base64.b64encode(str(datetime.now(timezone.utc).year).encode()).decode()

    health = registry.get_by_name(conn, "Health and Family Welfare Department")
    assert f"year={expected_year}" in health.url
    assert health.adapter == "tn_go_portal"


def test_deactivating_a_source_is_audited(conn, source_id):
    registry.set_active(conn, source_id, False, actor="operator")
    assert registry.get_source(conn, source_id).active is False
    entry = audit.trail(conn, entity_type="source", entity_id=source_id)[0]
    assert entry.action == "source.deactivated"
    assert entry.actor == "operator"
    assert (entry.before_value, entry.after_value) == ("true", "false")


def test_inactive_sources_are_not_crawled(conn, source_id, fetcher):
    from goengine.pipeline import run_discovery

    registry.set_active(conn, source_id, False)
    assert run_discovery(conn, fetcher, only_due=False) == []
    assert fetcher.requested == []
