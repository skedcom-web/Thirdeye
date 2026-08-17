from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from ..db import utcnow
from ..fetching import Fetcher, FetchError, HttpFetcher, Response

BUILTIN_TARGETS = [
    ("Google", "https://www.google.com"),
    ("Wikipedia", "https://www.wikipedia.org"),
    ("Tamil Nadu Government Home", "https://www.tn.gov.in"),
    ("Tamil Nadu GO Directory", "https://www.tn.gov.in/go_view/dept.php"),
]


def run_diagnostic_test(
    conn: sqlite3.Connection,
    target_name: str,
    url: str,
    fetcher: Fetcher,
) -> int:
    """Executes a single connectivity test and records the result in the database."""
    timestamp = utcnow()
    
    try:
        # Perform request with standard fetcher
        res = fetcher.get(url, verify=True, allow_fallback=True, enforce_policy=False)
        status = "SUCCESS"
        status_code = res.status_code
        response_time_ms = res.response_time_ms
        duration_ms = res.duration_ms
        response_size = len(res.content)
        content_type = res.content_type
        redirect_count = res.redirect_count
        ssl_verified = 1 if res.ssl_verified else 0
        user_agent = res.user_agent
        failure_category = ""
        failure_subtype = ""
        error_message = ""
        response_headers = json.dumps(res.headers)
        response_html = res.text[:2000]  # First 2000 characters
    except FetchError as exc:
        status = "FAILED"
        res = exc.response
        if res:
            status_code = res.status_code
            response_time_ms = res.response_time_ms
            duration_ms = res.duration_ms
            response_size = len(res.content)
            content_type = res.content_type
            redirect_count = res.redirect_count
            ssl_verified = 1 if res.ssl_verified else 0
            user_agent = res.user_agent
            failure_category = res.failure_category
            failure_subtype = res.failure_subtype
            error_message = res.error_message or str(exc)
            response_headers = json.dumps(res.headers)
            response_html = res.text[:2000]
        else:
            status_code = 0
            response_time_ms = 0.0
            duration_ms = 0.0
            response_size = 0
            content_type = ""
            redirect_count = 0
            ssl_verified = 1
            user_agent = ""
            failure_category = "network_failure"
            failure_subtype = "unknown_network"
            error_message = str(exc)
            response_headers = "{}"
            response_html = ""
    except Exception as exc:
        status = "FAILED"
        status_code = 0
        response_time_ms = 0.0
        duration_ms = 0.0
        response_size = 0
        content_type = ""
        redirect_count = 0
        ssl_verified = 1
        user_agent = ""
        failure_category = "unknown_failure"
        failure_subtype = ""
        error_message = str(exc)
        response_headers = "{}"
        response_html = ""

    cur = conn.execute(
        """
        INSERT INTO network_connectivity_tests
            (target_name, url, timestamp, status, status_code, response_time_ms, duration_ms,
             response_size, content_type, redirect_count, ssl_verified, user_agent,
             failure_category, failure_subtype, error_message, response_headers, response_html)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            target_name, url, timestamp, status, status_code, response_time_ms, duration_ms,
            response_size, content_type, redirect_count, ssl_verified, user_agent,
            failure_category, failure_subtype, error_message, response_headers, response_html
        )
    )
    return int(cur.lastrowid)


def run_all_diagnostics(
    conn: sqlite3.Connection,
    fetcher: Fetcher,
) -> list[int]:
    """Runs connectivity checks against all built-in targets and any registered sources."""
    test_ids = []
    
    # 1. Run all builtin targets
    for name, url in BUILTIN_TARGETS:
        test_ids.append(run_diagnostic_test(conn, name, url, fetcher))
        
    # 2. Run configured sources
    sources = conn.execute("SELECT id, name, url FROM sources WHERE active = 1").fetchall()
    for s in sources:
        name = f"Source: {s['name']}"
        test_ids.append(run_diagnostic_test(conn, name, s["url"], fetcher))
        
    return test_ids
