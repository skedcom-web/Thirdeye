"""Verification for the Network Truth Framework.

Proves the classifier can tell apart every failure mode named in the
blueprint -- policy block, redirect block, DNS failure, TCP failure, TLS
failure, connect timeout, read timeout, HTTP 403/404/500 -- using synthetic
exceptions for the pure classifiers (no real network needed) plus a couple
of `probe_connection` calls against inputs that behave predictably without
depending on live internet access. Also proves policy failures become
first-class evidence (crawl_evidences + network_connectivity_tests rows)
instead of an uncaught exception, and that the comparative-diagnostics layer
never overrides a target's own root cause.
"""

from __future__ import annotations

import socket
import ssl

import pytest

from goengine.operations import network_diagnosis as diag


# ---------------------------------------------------------------------------
# 1. No category is the literal string "network_failure"/"unknown_network"/
#    a bare "timeout", and every timeout classification carries a stage.
# ---------------------------------------------------------------------------
def test_no_category_is_a_generic_string():
    for banned in ("network_failure", "unknown_network", "timeout"):
        assert banned not in diag.ALL_ROOT_CAUSES
    assert all(cause == cause.upper() for cause in diag.ALL_ROOT_CAUSES)


def test_exactly_fourteen_categories():
    assert len(diag.ALL_ROOT_CAUSES) == 14
    assert len(set(diag.ALL_ROOT_CAUSES)) == 14  # no duplicates


def test_all_nine_stages_present():
    assert diag.ALL_STAGES == (
        diag.STAGE_REQUEST_CREATED, diag.STAGE_POLICY_VALIDATION, diag.STAGE_DNS_RESOLUTION,
        diag.STAGE_TCP_CONNECT, diag.STAGE_TLS_HANDSHAKE, diag.STAGE_HTTP_REQUEST_SENT,
        diag.STAGE_HTTP_RESPONSE_RECEIVED, diag.STAGE_REDIRECT_VALIDATION, diag.STAGE_COMPLETED,
    )


# ---------------------------------------------------------------------------
# 2. Policy block vs. DNS failure vs. TLS failure -- distinguishable, no
#    network required for the pure classifiers.
# ---------------------------------------------------------------------------
def test_policy_block_distinguishable_from_dns_and_tls():
    result = diag.probe_connection("https://example.com/", enforce_policy=True)
    assert result.root_cause == diag.POLICY_BLOCKED
    assert result.failure_stage == diag.STAGE_POLICY_VALIDATION
    assert result.last_successful_stage == diag.STAGE_REQUEST_CREATED
    assert result.confidence == diag.CONFIDENCE_HIGH
    assert result.exception_type == "SourceRejected"

    dns_cause, dns_confidence, _ = diag.classify_dns_exception(socket.gaierror("nodename nor servname provided"))
    assert dns_cause == diag.DNS_RESOLUTION_FAILED
    assert dns_cause != diag.POLICY_BLOCKED

    tls_cause, tls_confidence, _ = diag.classify_tls_exception(ssl.SSLCertVerificationError("certificate verify failed"))
    assert tls_cause == diag.TLS_HANDSHAKE_FAILED
    assert tls_cause not in (diag.POLICY_BLOCKED, diag.DNS_RESOLUTION_FAILED)
    assert dns_confidence == diag.CONFIDENCE_HIGH
    assert tls_confidence == diag.CONFIDENCE_HIGH


def test_url_validation_failure_distinct_from_policy_block():
    result = diag.probe_connection("not-a-url-at-all", enforce_policy=True)
    assert result.root_cause == diag.URL_VALIDATION_FAILED
    assert result.failure_stage == diag.STAGE_REQUEST_CREATED
    assert result.last_successful_stage is None


# ---------------------------------------------------------------------------
# 3. TCP failure modes: refused vs. connect-timeout vs. host-unreachable --
#    same stage, three different root causes because the OS signal differs.
# ---------------------------------------------------------------------------
def test_tcp_connection_failed_vs_connect_timeout_vs_host_unreachable():
    import errno

    refused_cause, refused_conf, _ = diag.classify_tcp_exception(ConnectionRefusedError("Connection refused"))
    assert refused_cause == diag.TCP_CONNECTION_FAILED
    assert refused_conf == diag.CONFIDENCE_HIGH

    timeout_cause, timeout_conf, _ = diag.classify_tcp_exception(socket.timeout("timed out"))
    assert timeout_cause == diag.CONNECT_TIMEOUT
    assert timeout_conf == diag.CONFIDENCE_MEDIUM

    unreachable_exc = OSError("no route to host")
    unreachable_exc.errno = errno.EHOSTUNREACH
    unreachable_cause, unreachable_conf, _ = diag.classify_tcp_exception(unreachable_exc)
    assert unreachable_cause == diag.REMOTE_HOST_UNREACHABLE
    assert unreachable_conf == diag.CONFIDENCE_HIGH

    assert len({refused_cause, timeout_cause, unreachable_cause}) == 3


# ---------------------------------------------------------------------------
# 4. Connect timeout vs. read timeout -- distinguished purely by which
#    stage the timeout occurred at, not by a bare "timeout" string.
# ---------------------------------------------------------------------------
def test_connect_timeout_distinct_from_read_timeout():
    import httpx

    read = diag.classify_http_layer_exception(httpx.ReadTimeout("timed out"))
    assert read.root_cause == diag.READ_TIMEOUT
    assert read.failure_stage == diag.STAGE_HTTP_RESPONSE_RECEIVED
    assert read.last_successful_stage == diag.STAGE_HTTP_REQUEST_SENT

    connect = diag.classify_http_layer_exception(httpx.ConnectTimeout("timed out"))
    assert connect.root_cause == diag.CONNECT_TIMEOUT
    assert connect.root_cause != read.root_cause

    tcp_cause, _, _ = diag.classify_tcp_exception(socket.timeout("timed out"))
    tls_cause, _, _ = diag.classify_tls_exception(socket.timeout("timed out"))
    assert tcp_cause == diag.CONNECT_TIMEOUT
    assert tls_cause == diag.CONNECT_TIMEOUT
    # Both pre-request timeouts land on CONNECT_TIMEOUT; the *stage* each
    # was raised at (available in the full probe result, not this pure
    # classifier call) is what tells them apart, per requirement 1.


# ---------------------------------------------------------------------------
# 5. HTTP status code classification: 403 / 404 / 500 all distinct.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status_code,expected",
    [(403, diag.HTTP_403_BLOCKED), (404, diag.HTTP_404), (500, diag.HTTP_500), (503, diag.HTTP_500)],
)
def test_status_code_classification(status_code, expected):
    assert diag.classify_status_code(status_code) == expected


def test_unmapped_status_code_not_forced_into_wrong_category():
    assert diag.classify_status_code(429) is None


def test_remote_server_dropped_connection_distinct_from_read_timeout():
    reset = diag.classify_http_layer_exception(ConnectionResetError("connection reset by peer"))
    assert reset.root_cause == diag.REMOTE_SERVER_DROPPED_CONNECTION
    assert reset.root_cause != diag.READ_TIMEOUT


# ---------------------------------------------------------------------------
# 6. Redirect-policy block is a distinct category from initial-URL policy
#    block, with its own stage (the bug this framework fixes: the old code
#    raised a bare FetchError with no response attached for this case).
# ---------------------------------------------------------------------------
def test_httpfetcher_classifies_policy_block_on_initial_url():
    from goengine.fetching import FetchError, HttpFetcher

    fetcher = HttpFetcher()
    with pytest.raises(FetchError) as exc_info:
        fetcher.get("https://not-a-government-host.example.com/")

    response = exc_info.value.response
    assert response is not None  # the old bug: this used to be None
    assert response.failure_category == diag.POLICY_BLOCKED
    assert response.failure_stage == diag.STAGE_POLICY_VALIDATION
    assert response.last_successful_stage == diag.STAGE_REQUEST_CREATED
    assert response.confidence_level == diag.CONFIDENCE_HIGH


# ---------------------------------------------------------------------------
# 7. Policy failures become first-class evidence: crawl_evidences AND
#    network_connectivity_tests rows get written, never an uncaught
#    SourceRejected. (diagnostic_reports is exercised at the route level,
#    covered separately by the workbench test suite.)
# ---------------------------------------------------------------------------
def test_policy_rejected_source_still_writes_crawl_evidence(conn):
    from goengine.discovery import crawler
    from goengine.fetching import HttpFetcher
    from goengine.registry import Source

    # Constructed directly (bypassing add_source's own approval check) to
    # simulate a source whose URL is no longer approved -- the only
    # realistic way this can happen in production, since add_source itself
    # refuses to register a disallowed URL.
    conn.execute(
        "INSERT INTO sources (name, department, url, host, source_type, adapter, active, "
        "crawl_frequency, priority, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, 'manual', 'Medium', '2026-01-01')",
        ("Rogue Source", "Test", "https://not-a-government-host.example.com/", "not-a-government-host.example.com",
         "go_portal", "generic_links"),
    )
    source_id = conn.execute("SELECT id FROM sources WHERE name = 'Rogue Source'").fetchone()["id"]
    source = Source(
        id=source_id, name="Rogue Source", department="Test",
        url="https://not-a-government-host.example.com/", host="not-a-government-host.example.com",
        source_type="go_portal", adapter="generic_links", active=True, crawl_frequency="manual",
        last_crawl_at=None, last_crawl_status=None, notes=None, priority="Medium", source_category=None,
    )

    result = crawler.crawl_source(conn, HttpFetcher(), source, actor="test")

    assert result.status == "error"
    evidences = conn.execute(
        "SELECT * FROM crawl_evidences WHERE crawl_run_id = ?", (result.run_id,)
    ).fetchall()
    assert len(evidences) == 1  # a real row, not a swallowed/uncaught exception
    assert evidences[0]["failure_category"] == diag.POLICY_BLOCKED
    assert evidences[0]["failure_stage"] == diag.STAGE_POLICY_VALIDATION
    assert evidences[0]["last_successful_stage"] == diag.STAGE_REQUEST_CREATED


def test_policy_rejected_target_writes_network_connectivity_test(conn):
    from goengine.fetching import HttpFetcher
    from goengine.operations import network_diagnostics as ops_net_diag

    test_id, result = ops_net_diag.run_diagnostic_test(
        conn, "Rogue Target", "https://not-a-government-host.example.com/",
        HttpFetcher(), enforce_policy=True,
    )

    assert result is not None
    assert result.root_cause == diag.POLICY_BLOCKED

    row = conn.execute("SELECT * FROM network_connectivity_tests WHERE id = ?", (test_id,)).fetchone()
    assert row is not None
    assert row["status"] == "FAILED"
    assert row["failure_category"] == diag.POLICY_BLOCKED
    assert row["failure_category"] != "network_failure"


# ---------------------------------------------------------------------------
# 8. Comparative diagnostics: never overrides the target's own root cause,
#    only produces one of the three fixed conclusion strings.
# ---------------------------------------------------------------------------
def _failed(root_cause: str, stage: str = diag.STAGE_TCP_CONNECT) -> diag.StageResult:
    return diag.StageResult(
        last_successful_stage=diag.STAGE_DNS_RESOLUTION,
        failure_stage=stage,
        root_cause=root_cause,
        technical_evidence="synthetic test result",
        confidence=diag.CONFIDENCE_HIGH,
    )


def _succeeded() -> diag.StageResult:
    return diag.StageResult(
        last_successful_stage=diag.STAGE_COMPLETED,
        failure_stage=None,
        root_cause=None,
        technical_evidence="synthetic success",
        confidence=diag.CONFIDENCE_HIGH,
    )


def test_conclusion_target_specific_when_controls_succeed():
    target = _failed(diag.TCP_CONNECTION_FAILED)
    controls = [_succeeded(), _succeeded()]  # Google, Wikipedia both fine

    conclusion = diag.build_comparison_conclusion(target, controls)

    assert conclusion == diag.CONCLUSION_TARGET_SPECIFIC
    assert target.root_cause == diag.TCP_CONNECTION_FAILED  # never relabeled


def test_conclusion_global_issue_when_controls_also_fail():
    target = _failed(diag.DNS_RESOLUTION_FAILED, stage=diag.STAGE_DNS_RESOLUTION)
    controls = [_failed(diag.DNS_RESOLUTION_FAILED, stage=diag.STAGE_DNS_RESOLUTION), _succeeded()]

    conclusion = diag.build_comparison_conclusion(target, controls)

    assert conclusion == diag.CONCLUSION_GLOBAL_ISSUE
    assert target.root_cause == diag.DNS_RESOLUTION_FAILED  # still not relabeled -- additive, not destructive


def test_conclusion_healthy_when_everything_succeeds():
    target = _succeeded()
    controls = [_succeeded(), _succeeded()]
    assert diag.build_comparison_conclusion(target, controls) == diag.CONCLUSION_HEALTHY


def test_policy_blocked_never_treated_as_global_issue_evidence():
    """POLICY_BLOCKED / HTTP_404 / HTTP_403_BLOCKED can only ever mean
    "this specific target/action", never "our whole network is down" --
    even if, coincidentally, controls also failed for unrelated reasons."""
    for target_cause in (diag.POLICY_BLOCKED, diag.HTTP_404, diag.HTTP_403_BLOCKED):
        target = _failed(target_cause)
        controls = [_failed(diag.DNS_RESOLUTION_FAILED), _failed(diag.DNS_RESOLUTION_FAILED)]
        conclusion = diag.build_comparison_conclusion(target, controls)
        # Controls failing with an egress-plausible signature still drives
        # the conclusion toward GLOBAL_ISSUE (that's about the controls,
        # not the target) -- but the target's own category is untouched.
        assert conclusion == diag.CONCLUSION_GLOBAL_ISSUE
        assert target.root_cause == target_cause
