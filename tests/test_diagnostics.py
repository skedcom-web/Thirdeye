import pytest
import sqlite3
import os
from goengine.db import init_db, utcnow
from goengine.config import Settings
from goengine.fetching import OfflineFetcher, Response, FetchError, HttpFetcher
from goengine.discovery import crawler
from goengine.operations import sources as ops_sources
from goengine.operations import jobs as ops_jobs
from goengine.workbench.operations_routes import _get_failure_stats


@pytest.fixture
def conn(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test.db",
        repository_dir=tmp_path / "repo",
    )
    # Ensure PYTEST_CURRENT_TEST is set to skip TN seeding
    os.environ["PYTEST_CURRENT_TEST"] = "true"
    db_conn = init_db(settings)
    yield db_conn
    db_conn.close()


def test_source_ssl_settings_default_and_edit(conn):
    # Test creating a source has SSL verification enabled by default
    source_id = ops_sources.create_source(
        conn,
        name="Test SSL Source",
        department="Health",
        url="https://tn.gov.in/test",
        source_type="go_portal",
        actor="test-actor",
    )
    
    source = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    assert bool(source["ssl_verification_enabled"]) is True
    assert bool(source["allow_ssl_fallback"]) is False

    # Test updating SSL settings
    ops_sources.edit_source(
        conn,
        source_id,
        ssl_verification_enabled=False,
        allow_ssl_fallback=True,
        actor="test-actor",
        reason="Update SSL policies",
    )
    
    source = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    assert bool(source["ssl_verification_enabled"]) is False
    assert bool(source["allow_ssl_fallback"]) is True


def test_crawl_evidence_logging(conn):
    # Create source
    source_id = ops_sources.create_source(
        conn,
        name="Test Evidence Source",
        department="Education",
        url="https://tn.gov.in/evidence",
        source_type="go_portal",
        actor="test-actor",
    )
    
    # Configure OfflineFetcher with telemetry responses
    fetcher = OfflineFetcher()
    fetcher.add_html("https://tn.gov.in/evidence", "<html></html>")
    
    # Run crawl
    from goengine import registry
    source_obj = registry.get_source(conn, source_id)
    crawl_result = crawler.crawl_source(conn, fetcher, source_obj)
    
    # Check that crawl evidence was recorded
    evidences = conn.execute("SELECT * FROM crawl_evidences WHERE crawl_run_id = ?", (crawl_result.run_id,)).fetchall()
    assert len(evidences) == 1
    ev = evidences[0]
    assert ev["url"] == "https://tn.gov.in/evidence"
    assert ev["status_code"] == 200
    assert ev["ssl_verified"] == 1


def test_diagnostics_retention_policy(conn):
    # Create source and crawl run first to satisfy FK constraints
    source_id = ops_sources.create_source(
        conn,
        name="Test SSL Source",
        department="Health",
        url="https://tn.gov.in/test",
        source_type="go_portal",
        actor="test-actor",
    )
    conn.execute(
        "INSERT INTO crawl_runs (id, source_id, started_at, status) VALUES (1, ?, ?, 'ok')",
        (source_id, utcnow())
    )

    # Seed crawl evidences
    conn.execute(
        """
        INSERT INTO crawl_evidences
            (crawl_run_id, url, timestamp, ssl_verified)
        VALUES (1, 'https://tn.gov.in/old', '2026-05-01T12:00:00', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO crawl_evidences
            (crawl_run_id, url, timestamp, ssl_verified)
        VALUES (1, 'https://tn.gov.in/new', ?, 1)
        """,
        (utcnow(),)
    )

    # Set retention period to 10 days
    conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('diagnostics_retention_days', '10')")

    # Run cleanup
    deleted = ops_jobs.cleanup_expired_evidence(conn)
    assert deleted == 1

    # Verify old evidence was deleted but new remains
    remaining = conn.execute("SELECT url FROM crawl_evidences ORDER BY id").fetchall()
    assert len(remaining) == 1
    assert remaining[0]["url"] == "https://tn.gov.in/new"


def test_failure_classification_aggregation(conn):
    # Create source and crawl run first to satisfy FK constraints
    source_id = ops_sources.create_source(
        conn,
        name="Test SSL Source",
        department="Health",
        url="https://tn.gov.in/test",
        source_type="go_portal",
        actor="test-actor",
    )
    conn.execute(
        "INSERT INTO crawl_runs (id, source_id, started_at, status) VALUES (1, ?, ?, 'ok')",
        (source_id, utcnow())
    )

    # Insert failed crawl evidences
    conn.execute(
        """
        INSERT INTO crawl_evidences
            (crawl_run_id, url, timestamp, failure_category, failure_subtype)
        VALUES (1, 'https://tn.gov.in/fail1', ?, 'network_failure', 'timeout')
        """,
        (utcnow(),)
    )
    conn.execute(
        """
        INSERT INTO crawl_evidences
            (crawl_run_id, url, timestamp, failure_category, failure_subtype)
        VALUES (1, 'https://tn.gov.in/fail2', ?, 'network_failure', 'ssl_error')
        """,
        (utcnow(),)
    )

    # Get failure statistics
    stats = _get_failure_stats(conn, 30)
    assert stats["network"] == 2


def test_db_seeding_tamil_nadu(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "test_seed.db",
        repository_dir=tmp_path / "repo_seed",
    )
    
    # Temporarily remove PYTEST_CURRENT_TEST to trigger seeding
    old_env = os.environ.get("PYTEST_CURRENT_TEST")
    if old_env:
        del os.environ["PYTEST_CURRENT_TEST"]
    
    try:
        db_conn = init_db(settings)
        # Check that Tamil Nadu state was created
        state = db_conn.execute("SELECT * FROM states WHERE code = 'TN'").fetchone()
        assert state is not None
        assert state["name"] == "Tamil Nadu"
        
        # Check that districts were seeded (should be 38)
        districts_count = db_conn.execute("SELECT COUNT(*) AS n FROM districts WHERE state_id = ?", (state["id"],)).fetchone()["n"]
        assert districts_count == 38
        
        # Check that sources were seeded
        sources_count = db_conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
        assert sources_count > 0
        db_conn.close()
    finally:
        if old_env:
            os.environ["PYTEST_CURRENT_TEST"] = old_env
