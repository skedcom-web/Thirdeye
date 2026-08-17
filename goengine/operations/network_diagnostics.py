from __future__ import annotations

import json
import sqlite3
from . import network_diagnosis as diag
from ..db import utcnow
from ..fetching import Fetcher, FetchError, HttpFetcher, Response

# Known-good, always-off-policy hosts used purely as control targets: if
# these also fail in the same diagnostic run, the honest conclusion is that
# our own egress (Render's network) is the problem, not TN government
# infrastructure blocking us specifically. See
# network_diagnosis.build_comparison_conclusion. This never overrides a
# target's own root-cause category (requirement 5) -- it only produces an
# additional conclusion string stored alongside it.
CONTROL_TARGET_NAMES = frozenset({"Google", "Wikipedia", "Example.com"})

BUILTIN_TARGETS = [
    ("Google", "https://www.google.com"),
    ("Wikipedia", "https://www.wikipedia.org"),
    ("Example.com", "https://www.example.com"),
    ("Tamil Nadu Government Home", "https://www.tn.gov.in"),
    ("Tamil Nadu GO Directory", "https://www.tn.gov.in/go_view/dept.php"),
]


def run_diagnostic_test(
    conn: sqlite3.Connection,
    target_name: str,
    url: str,
    fetcher: Fetcher,
    *,
    enforce_policy: bool = False,
) -> tuple[int, diag.StageResult | None]:
    """Executes a single connectivity test and records the result.

    Returns (row_id, stage_result) -- `stage_result.ok` is True on success
    (there is no root cause to report, but the result is still needed so
    `run_all_diagnostics` can compute the comparative conclusion across the
    whole batch, which requires knowing whether *every* target, including
    successful ones, actually completed)."""
    timestamp = utcnow()
    stage_result: diag.StageResult | None = None

    try:
        res = fetcher.get(url, verify=True, allow_fallback=True, enforce_policy=enforce_policy)
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
        response_html = res.text[:2000]
        last_successful_stage = diag.STAGE_COMPLETED
        failure_stage = None
        confidence_level = ""
        exception_type = ""
        exception_message = ""
        stage_result = diag.StageResult(
            last_successful_stage=diag.STAGE_COMPLETED, failure_stage=None,
            root_cause=None, technical_evidence="request succeeded", confidence=diag.CONFIDENCE_HIGH,
        )
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
            failure_category = res.failure_category or diag.UNKNOWN_FAILURE
            failure_subtype = res.failure_subtype or failure_category
            error_message = res.error_message or str(exc)
            response_headers = json.dumps(res.headers)
            response_html = res.text[:2000]
            last_successful_stage = res.last_successful_stage
            failure_stage = res.failure_stage
            confidence_level = res.confidence_level or diag.CONFIDENCE_LOW
            exception_type = res.exception_type
            exception_message = res.exception_message
        else:
            status_code = 0
            response_time_ms = 0.0
            duration_ms = 0.0
            response_size = 0
            content_type = ""
            redirect_count = 0
            ssl_verified = 1
            user_agent = ""
            failure_category = diag.UNKNOWN_FAILURE
            failure_subtype = diag.UNKNOWN_FAILURE
            error_message = str(exc)
            response_headers = "{}"
            response_html = ""
            last_successful_stage = None
            failure_stage = None
            confidence_level = diag.CONFIDENCE_LOW
            exception_type = type(exc).__name__
            exception_message = str(exc)
        stage_result = diag.StageResult(
            last_successful_stage=last_successful_stage, failure_stage=failure_stage,
            root_cause=failure_category, technical_evidence=error_message,
            confidence=confidence_level or diag.CONFIDENCE_LOW,
            exception_type=exception_type, exception_message=exception_message,
        )
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
        failure_category = diag.UNKNOWN_FAILURE
        failure_subtype = diag.UNKNOWN_FAILURE
        error_message = str(exc)
        response_headers = "{}"
        response_html = ""
        last_successful_stage = None
        failure_stage = None
        confidence_level = diag.CONFIDENCE_LOW
        exception_type = type(exc).__name__
        exception_message = str(exc)
        stage_result = diag.StageResult(
            last_successful_stage=None, failure_stage=None,
            root_cause=diag.UNKNOWN_FAILURE, technical_evidence=f"unclassified exception: {exc!r}",
            confidence=diag.CONFIDENCE_LOW, exception_type=exception_type, exception_message=exception_message,
        )

    cur = conn.execute(
        """
        INSERT INTO network_connectivity_tests
            (target_name, url, timestamp, status, status_code, response_time_ms, duration_ms,
             response_size, content_type, redirect_count, ssl_verified, user_agent,
             failure_category, failure_subtype, error_message, response_headers, response_html,
             last_successful_stage, failure_stage, confidence_level,
             exception_type, exception_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            target_name, url, timestamp, status, status_code, response_time_ms, duration_ms,
            response_size, content_type, redirect_count, ssl_verified, user_agent,
            failure_category, failure_subtype, error_message, response_headers, response_html,
            last_successful_stage, failure_stage, confidence_level,
            exception_type, exception_message
        )
    )
    return int(cur.lastrowid), stage_result


def run_all_diagnostics(
    conn: sqlite3.Connection,
    fetcher: Fetcher,
) -> list[int]:
    """Runs connectivity checks against all built-in targets and any active
    registered sources, then computes a comparative conclusion for every
    non-control target: "Render outbound connectivity healthy",
    "Target-specific failure detected", or "Global network issue detected"
    -- based only on whether the control targets (Google/Wikipedia/
    Example.com) also failed in this same run. The target's own
    `failure_category` is never rewritten by this step."""
    test_ids: list[int] = []
    control_results: list[diag.StageResult] = []
    target_rows: list[tuple[int, diag.StageResult | None]] = []

    for name, url in BUILTIN_TARGETS:
        row_id, result = run_diagnostic_test(conn, name, url, fetcher, enforce_policy=False)
        test_ids.append(row_id)
        if name in CONTROL_TARGET_NAMES and result is not None:
            control_results.append(result)
        elif result is not None:
            target_rows.append((row_id, result))

    sources = conn.execute("SELECT id, name, url FROM sources WHERE active = 1").fetchall()
    for s in sources:
        name = f"Source: {s['name']}"
        row_id, result = run_diagnostic_test(conn, name, s["url"], fetcher, enforce_policy=True)
        test_ids.append(row_id)
        if result is not None:
            target_rows.append((row_id, result))

    for row_id, result in target_rows:
        conclusion = diag.build_comparison_conclusion(result, control_results)
        conn.execute(
            "UPDATE network_connectivity_tests SET comparison_conclusion = ? WHERE id = ?",
            (conclusion, row_id),
        )

    return test_ids
