import pytest
import sqlite3
import json
import os
from pathlib import Path
from fastapi.testclient import TestClient
from goengine.db import init_db, utcnow
from goengine.config import Settings
from goengine.fetching import OfflineFetcher, Response, FetchError
from goengine.operations import network_diagnosis as diag
from goengine.operations import network_diagnostics as ops_net_diag
from goengine.operations import sources as ops_sources
from goengine.workbench.app import create_app
from tests.conftest import login_as


@pytest.fixture
def client(conn, settings):
    test_client = TestClient(create_app(settings))
    login_as(test_client, conn)
    return test_client





def test_run_diagnostic_test_success(conn):
    fetcher = OfflineFetcher()
    fetcher.add_html("https://www.google.com", "<html>Google</html>")
    
    test_id, result = ops_net_diag.run_diagnostic_test(
        conn, "Google", "https://www.google.com", fetcher
    )
    assert result is not None
    assert result.ok  # no root cause to report on success, but the result itself is real

    test = conn.execute("SELECT * FROM network_connectivity_tests WHERE id = ?", (test_id,)).fetchone()
    assert test is not None
    assert test["target_name"] == "Google"
    assert test["url"] == "https://www.google.com"
    assert test["status"] == "SUCCESS"
    assert test["status_code"] == 200
    assert "Google" in test["response_html"]
    
    # Verify response headers parsed
    headers = json.loads(test["response_headers"])
    assert headers["content-type"] == "text/html; charset=utf-8"


def test_run_diagnostic_test_failure(conn):
    fetcher = OfflineFetcher()
    # Trigger KeyError to simulate connection error / FetchError
    # OfflineFetcher get raises FetchError for unregistered URLs
    
    test_id, result = ops_net_diag.run_diagnostic_test(
        conn, "Failure Target", "https://does-not-exist.example.com", fetcher
    )
    assert result is not None
    assert result.root_cause == diag.UNKNOWN_FAILURE  # never the old literal "network_failure"

    test = conn.execute("SELECT * FROM network_connectivity_tests WHERE id = ?", (test_id,)).fetchone()
    assert test is not None
    assert test["target_name"] == "Failure Target"
    assert test["status"] == "FAILED"
    assert test["status_code"] == 0
    assert test["failure_category"] != "network_failure"
    assert test["failure_category"] == diag.UNKNOWN_FAILURE
    assert "no offline response" in test["error_message"]


def test_run_all_diagnostics(conn):
    # Register an active source
    source_id = ops_sources.create_source(
        conn,
        name="Source active",
        department="Education",
        url="https://tn.gov.in/edu",
        source_type="go_portal",
        actor="test-actor",
    )
    
    # Configure offline responses for all targets
    fetcher = OfflineFetcher()
    fetcher.add_html("https://www.google.com", "Google")
    fetcher.add_html("https://www.wikipedia.org", "Wikipedia")
    fetcher.add_html("https://www.example.com", "Example")
    fetcher.add_html("https://www.tn.gov.in", "TN Home")
    fetcher.add_html("https://www.tn.gov.in/go_view/dept.php", "TN Directory")
    fetcher.add_html("https://tn.gov.in/edu", "Source URL")

    test_ids = ops_net_diag.run_all_diagnostics(conn, fetcher)

    # Built-in (5, including the Example.com control target) + Active Sources (1) = 6 tests
    assert len(test_ids) == 6

    # Every target's comparison_conclusion should say healthy, since every
    # control and every target succeeded in this run.
    conclusions = conn.execute("SELECT comparison_conclusion FROM network_connectivity_tests").fetchall()
    non_control_conclusions = [c["comparison_conclusion"] for c in conclusions if c["comparison_conclusion"] is not None]
    assert all(c == diag.CONCLUSION_HEALTHY for c in non_control_conclusions)

    tests = conn.execute("SELECT target_name FROM network_connectivity_tests ORDER BY id").fetchall()
    names = [t["target_name"] for t in tests]
    assert "Google" in names
    assert "Wikipedia" in names
    assert "Tamil Nadu Government Home" in names
    assert "Tamil Nadu GO Directory" in names
    assert "Source: Source active" in names


def test_diagnostics_ui_endpoints(client, conn):
    # Run tests POST
    res_run = client.post("/ops/diagnostics/network/run", follow_redirects=False)
    assert res_run.status_code == 303
    
    # Get dashboard GET
    res_dashboard = client.get("/ops/diagnostics/network")
    assert res_dashboard.status_code == 200
    assert b"Network Connectivity" in res_dashboard.content
    assert b"Google" in res_dashboard.content

    # Insert a dummy successful test row with HTML content to test inspection and download
    cur = conn.execute(
        """
        INSERT INTO network_connectivity_tests (target_name, url, timestamp, status, status_code, response_html)
        VALUES ('Test Download', 'http://example.com', '2026-08-17 12:00:00', 'SUCCESS', 200, '<html>dummy</html>')
        """
    )
    test_id = cur.lastrowid
    conn.commit()

    # View details GET
    res_detail = client.get(f"/ops/diagnostics/network/{test_id}")
    assert res_detail.status_code == 200
    assert b"Target Metadata" in res_detail.content

    # Download GET
    res_download = client.get(f"/ops/diagnostics/network/{test_id}/download")
    assert res_download.status_code == 200
    assert "attachment" in res_download.headers["content-disposition"]
