"""Network Truth Framework -- definitive root-cause classification.

Every network failure the crawler or the diagnostics tools observe reduces
to exactly one of the fourteen categories in `ALL_ROOT_CAUSES`, plus the
pipeline stage execution actually reached before it failed. "network_failure",
"unknown_network", and a bare "timeout" with no stage attached are not valid
outputs of this module -- if that string appears anywhere as a *final*
classification, this framework has a bug.

Design note on *why* a separate stdlib-socket prober exists instead of just
inspecting httpx's own exceptions more carefully: httpx's timeout model
bundles DNS resolution + TCP connect + TLS handshake into a single "connect"
phase -- a `httpx.ConnectTimeout` alone cannot tell you which of the three
actually stalled, and `httpx.ConnectError`'s underlying cause varies by
platform in ways that make string-matching the error text unreliable (this
codebase used to do exactly that, matching "getaddrinfo" in the exception
text, which is OS/libc-dependent). `socket.gaierror`, `ConnectionRefusedError`,
and `ssl.SSLError` are unambiguous by *type*, not by message text, when each
stage is attempted as a separate, explicit step. That is what `probe_connection`
does. It does not perform the actual HTTP request/response -- that stays in
HttpFetcher via httpx, since duplicating httpx's connection pooling and
streaming here would be its own source of bugs. The probe exists purely to
answer, after httpx has already failed, "which stage actually broke."

Design note on comparative diagnostics (requirement 5): the control-target
comparison never overrides a target's own root-cause category. A target
that failed with DNS_RESOLUTION_FAILED stays DNS_RESOLUTION_FAILED even if
Google also failed in the same run -- the *evidence* for "this might be our
own egress, not the target" is additive context (`build_comparison_conclusion`),
not a relabeling of what was directly observed.
"""

from __future__ import annotations

import socket
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..registry import SourceRejected, assert_approved

# ---------------------------------------------------------------------------
# Pipeline stages, in the order a request actually passes through them.
# ---------------------------------------------------------------------------
STAGE_REQUEST_CREATED = "REQUEST_CREATED"
STAGE_POLICY_VALIDATION = "POLICY_VALIDATION"
STAGE_DNS_RESOLUTION = "DNS_RESOLUTION"
STAGE_TCP_CONNECT = "TCP_CONNECT"
STAGE_TLS_HANDSHAKE = "TLS_HANDSHAKE"
STAGE_HTTP_REQUEST_SENT = "HTTP_REQUEST_SENT"
STAGE_HTTP_RESPONSE_RECEIVED = "HTTP_RESPONSE_RECEIVED"
STAGE_REDIRECT_VALIDATION = "REDIRECT_VALIDATION"
STAGE_COMPLETED = "COMPLETED"

ALL_STAGES = (
    STAGE_REQUEST_CREATED, STAGE_POLICY_VALIDATION, STAGE_DNS_RESOLUTION,
    STAGE_TCP_CONNECT, STAGE_TLS_HANDSHAKE, STAGE_HTTP_REQUEST_SENT,
    STAGE_HTTP_RESPONSE_RECEIVED, STAGE_REDIRECT_VALIDATION, STAGE_COMPLETED,
)

# ---------------------------------------------------------------------------
# Root cause categories -- exactly these fourteen.
# ---------------------------------------------------------------------------
POLICY_BLOCKED = "POLICY_BLOCKED"
URL_VALIDATION_FAILED = "URL_VALIDATION_FAILED"
REDIRECT_POLICY_BLOCKED = "REDIRECT_POLICY_BLOCKED"
DNS_RESOLUTION_FAILED = "DNS_RESOLUTION_FAILED"
TCP_CONNECTION_FAILED = "TCP_CONNECTION_FAILED"
TLS_HANDSHAKE_FAILED = "TLS_HANDSHAKE_FAILED"
CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
READ_TIMEOUT = "READ_TIMEOUT"
HTTP_403_BLOCKED = "HTTP_403_BLOCKED"
HTTP_404 = "HTTP_404"
HTTP_500 = "HTTP_500"
REMOTE_SERVER_DROPPED_CONNECTION = "REMOTE_SERVER_DROPPED_CONNECTION"
REMOTE_HOST_UNREACHABLE = "REMOTE_HOST_UNREACHABLE"
UNKNOWN_FAILURE = "UNKNOWN_FAILURE"

ALL_ROOT_CAUSES = (
    POLICY_BLOCKED, URL_VALIDATION_FAILED, REDIRECT_POLICY_BLOCKED,
    DNS_RESOLUTION_FAILED, TCP_CONNECTION_FAILED, TLS_HANDSHAKE_FAILED,
    CONNECT_TIMEOUT, READ_TIMEOUT, HTTP_403_BLOCKED, HTTP_404, HTTP_500,
    REMOTE_SERVER_DROPPED_CONNECTION, REMOTE_HOST_UNREACHABLE, UNKNOWN_FAILURE,
)

# Categories the comparison layer treats as "could plausibly mean our own
# egress is broken" when deciding what conclusion to draw -- never used to
# relabel the target's own category, only to select the conclusion string.
EGRESS_PLAUSIBLE_CATEGORIES = frozenset({
    DNS_RESOLUTION_FAILED, TCP_CONNECTION_FAILED, TLS_HANDSHAKE_FAILED,
    CONNECT_TIMEOUT, REMOTE_HOST_UNREACHABLE,
})

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"

CONCLUSION_HEALTHY = "Render outbound connectivity healthy"
CONCLUSION_TARGET_SPECIFIC = "Target-specific failure detected"
CONCLUSION_GLOBAL_ISSUE = "Global network issue detected"


@dataclass
class StageResult:
    """One definitive classification. `failure_stage is None` means every
    stage this function is responsible for succeeded -- the caller (usually
    HttpFetcher, after an httpx-level exception) should then attribute the
    failure to the HTTP layer instead, using `classify_http_layer_exception`."""
    last_successful_stage: str | None
    failure_stage: str | None
    root_cause: str | None
    technical_evidence: str
    confidence: str
    exception_type: str = ""
    exception_message: str = ""
    stage_timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.failure_stage is None


# ---------------------------------------------------------------------------
# Pure classifiers -- take an exception instance, return (root_cause,
# confidence, evidence). No I/O, fully unit-testable with synthetic
# exceptions constructed directly (no real socket needed).
# ---------------------------------------------------------------------------
def classify_dns_exception(exc: BaseException) -> tuple[str, str, str]:
    if isinstance(exc, socket.gaierror):
        return DNS_RESOLUTION_FAILED, CONFIDENCE_HIGH, f"socket.gaierror: {exc}"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return DNS_RESOLUTION_FAILED, CONFIDENCE_MEDIUM, f"DNS query did not complete in time: {exc}"
    return DNS_RESOLUTION_FAILED, CONFIDENCE_LOW, f"unexpected exception during DNS resolution: {exc!r}"


def classify_tcp_exception(exc: BaseException) -> tuple[str, str, str]:
    """TCP-stage failures split three ways by exception *type*, not timing
    heuristics: an explicit refusal (HIGH confidence -- the OS told us
    definitively), our own timeout budget expiring (CONNECT_TIMEOUT --
    inherently ambiguous between "host down" and "firewall black-holing",
    hence MEDIUM), or an OS-level "no route" signal (HIGH -- also
    definitive, just a different definitive answer than "refused")."""
    if isinstance(exc, ConnectionRefusedError):
        return TCP_CONNECTION_FAILED, CONFIDENCE_HIGH, f"ConnectionRefusedError: {exc} (target reachable, nothing accepting on that port)"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return CONNECT_TIMEOUT, CONFIDENCE_MEDIUM, (
            f"TCP SYN sent, no response within timeout: {exc} -- "
            "ambiguous between host down and a firewall silently dropping packets"
        )
    if isinstance(exc, OSError):
        import errno as errno_module
        evidence = getattr(exc, "strerror", None) or str(exc)
        if exc.errno in (errno_module.ENETUNREACH, errno_module.EHOSTUNREACH):
            return REMOTE_HOST_UNREACHABLE, CONFIDENCE_HIGH, f"OS reports no route to host: {evidence}"
        return TCP_CONNECTION_FAILED, CONFIDENCE_MEDIUM, f"OSError during TCP connect: {evidence}"
    return TCP_CONNECTION_FAILED, CONFIDENCE_LOW, f"unexpected exception during TCP connect: {exc!r}"


def classify_tls_exception(exc: BaseException) -> tuple[str, str, str]:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return TLS_HANDSHAKE_FAILED, CONFIDENCE_HIGH, f"certificate verification failed: {exc}"
    if isinstance(exc, ssl.SSLError):
        return TLS_HANDSHAKE_FAILED, CONFIDENCE_HIGH, f"SSLError: {exc}"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return CONNECT_TIMEOUT, CONFIDENCE_MEDIUM, f"TLS handshake started but did not complete in time: {exc}"
    return TLS_HANDSHAKE_FAILED, CONFIDENCE_LOW, f"unexpected exception during TLS handshake: {exc!r}"


def classify_status_code(status_code: int) -> str | None:
    """Maps an HTTP status code to a root cause. Returns None for codes not
    in the closed fourteen-category list (e.g. 401, 429) -- callers should
    fall back to UNKNOWN_FAILURE and record the literal code as evidence
    rather than force-fitting it into an unrelated category."""
    if status_code == 403:
        return HTTP_403_BLOCKED
    if status_code == 404:
        return HTTP_404
    if 500 <= status_code < 600:
        return HTTP_500
    return None


# ---------------------------------------------------------------------------
# The staged prober itself.
# ---------------------------------------------------------------------------
def probe_connection(
    url: str,
    *,
    connect_timeout: float = 10.0,
    tls_timeout: float = 10.0,
    enforce_policy: bool = True,
) -> StageResult:
    """Walks REQUEST_CREATED -> POLICY_VALIDATION -> DNS_RESOLUTION ->
    TCP_CONNECT -> TLS_HANDSHAKE by hand, using stdlib `socket`/`ssl`
    directly rather than httpx.

    `enforce_policy=False` is for control targets (e.g. google.com) used
    purely to test whether egress works at all -- they are never on the
    government-source allowlist and are not meant to be crawled."""
    timings: dict[str, float] = {}

    # Stage: REQUEST_CREATED (URL parsing/validation happens here).
    t0 = time.perf_counter()
    parsed = urlparse(url)
    host = parsed.hostname
    if parsed.scheme not in ("http", "https") or not host:
        return StageResult(
            last_successful_stage=None,
            failure_stage=STAGE_REQUEST_CREATED,
            root_cause=URL_VALIDATION_FAILED,
            technical_evidence=f"URL is not a valid http(s) URL with a host: {url!r}",
            confidence=CONFIDENCE_HIGH,
            exception_type="ValueError",
            exception_message=f"invalid URL: {url!r}",
            stage_timings_ms=timings,
        )
    timings[STAGE_REQUEST_CREATED] = (time.perf_counter() - t0) * 1000.0

    # Stage: POLICY_VALIDATION.
    t0 = time.perf_counter()
    if enforce_policy:
        try:
            assert_approved(url)
        except SourceRejected as exc:
            return StageResult(
                last_successful_stage=STAGE_REQUEST_CREATED,
                failure_stage=STAGE_POLICY_VALIDATION,
                root_cause=POLICY_BLOCKED,
                technical_evidence=f"SourceRejected: {exc}",
                confidence=CONFIDENCE_HIGH,
                exception_type=type(exc).__name__,
                exception_message=str(exc),
                stage_timings_ms=timings,
            )
    timings[STAGE_POLICY_VALIDATION] = (time.perf_counter() - t0) * 1000.0

    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    # Stage: DNS_RESOLUTION.
    t0 = time.perf_counter()
    try:
        addrinfo = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, socket.timeout, TimeoutError) as exc:
        root_cause, confidence, evidence = classify_dns_exception(exc)
        return StageResult(
            last_successful_stage=STAGE_POLICY_VALIDATION,
            failure_stage=STAGE_DNS_RESOLUTION,
            root_cause=root_cause,
            technical_evidence=evidence,
            confidence=confidence,
            exception_type=type(exc).__name__,
            exception_message=str(exc),
            stage_timings_ms=timings,
        )
    timings[STAGE_DNS_RESOLUTION] = (time.perf_counter() - t0) * 1000.0
    resolved_ip = addrinfo[0][4][0] if addrinfo else None

    # Stage: TCP_CONNECT.
    t0 = time.perf_counter()
    sock: socket.socket | None = None
    try:
        sock = socket.create_connection((host, port), timeout=connect_timeout)
    except (ConnectionRefusedError, socket.timeout, TimeoutError, OSError) as exc:
        root_cause, confidence, evidence = classify_tcp_exception(exc)
        return StageResult(
            last_successful_stage=STAGE_DNS_RESOLUTION,
            failure_stage=STAGE_TCP_CONNECT,
            root_cause=root_cause,
            technical_evidence=f"{evidence} (resolved to {resolved_ip})",
            confidence=confidence,
            exception_type=type(exc).__name__,
            exception_message=str(exc),
            stage_timings_ms=timings,
        )
    timings[STAGE_TCP_CONNECT] = (time.perf_counter() - t0) * 1000.0

    try:
        # Stage: TLS_HANDSHAKE (HTTPS only).
        if parsed.scheme == "https":
            t0 = time.perf_counter()
            try:
                sock.settimeout(tls_timeout)
                ctx = ssl.create_default_context()
                with ctx.wrap_socket(sock, server_hostname=host):
                    pass
            except (ssl.SSLError, socket.timeout, TimeoutError) as exc:
                root_cause, confidence, evidence = classify_tls_exception(exc)
                return StageResult(
                    last_successful_stage=STAGE_TCP_CONNECT,
                    failure_stage=STAGE_TLS_HANDSHAKE,
                    root_cause=root_cause,
                    technical_evidence=evidence,
                    confidence=confidence,
                    exception_type=type(exc).__name__,
                    exception_message=str(exc),
                    stage_timings_ms=timings,
                )
            timings[STAGE_TLS_HANDSHAKE] = (time.perf_counter() - t0) * 1000.0
            last_stage = STAGE_TLS_HANDSHAKE
        else:
            last_stage = STAGE_TCP_CONNECT
    finally:
        try:
            sock.close()
        except OSError:
            pass

    # Every stage this function is responsible for succeeded. The caller
    # must attribute the original failure to the HTTP layer.
    return StageResult(
        last_successful_stage=last_stage,
        failure_stage=None,
        root_cause=None,
        technical_evidence=(
            f"REQUEST_CREATED, POLICY_VALIDATION, DNS_RESOLUTION ({resolved_ip}), "
            f"TCP_CONNECT, and TLS_HANDSHAKE (if applicable) all succeeded on this probe."
        ),
        confidence=CONFIDENCE_HIGH,
        stage_timings_ms=timings,
    )


# ---------------------------------------------------------------------------
# HTTP-layer classification -- used once `probe_connection` reports the
# network layer is fine, so the failure must be in the request/response
# itself. Also pure/unit-testable: takes the original exception + an
# optional status code, no I/O.
# ---------------------------------------------------------------------------
def classify_http_layer_exception(
    exc: BaseException | None,
    *,
    status_code: int | None = None,
    probe: StageResult | None = None,
) -> StageResult:
    import httpx  # local import: only needed for isinstance checks here

    if status_code is not None:
        root_cause = classify_status_code(status_code)
        return StageResult(
            last_successful_stage=STAGE_HTTP_RESPONSE_RECEIVED,
            failure_stage=STAGE_REDIRECT_VALIDATION if root_cause is None else None,
            root_cause=root_cause or UNKNOWN_FAILURE,
            technical_evidence=f"HTTP status {status_code}" + ("" if root_cause else " (not in the closed root-cause list)"),
            confidence=CONFIDENCE_HIGH if root_cause else CONFIDENCE_LOW,
            exception_type="HTTPStatusError",
            exception_message=f"HTTP {status_code}",
        )

    if isinstance(exc, httpx.ReadTimeout):
        return StageResult(
            last_successful_stage=STAGE_HTTP_REQUEST_SENT,
            failure_stage=STAGE_HTTP_RESPONSE_RECEIVED,
            root_cause=READ_TIMEOUT,
            technical_evidence=f"httpx.ReadTimeout after the request was sent and the connection was established: {exc}",
            confidence=CONFIDENCE_HIGH,
            exception_type=type(exc).__name__,
            exception_message=str(exc),
        )

    if isinstance(exc, (httpx.RemoteProtocolError, ConnectionResetError)):
        return StageResult(
            last_successful_stage=STAGE_HTTP_REQUEST_SENT,
            failure_stage=STAGE_HTTP_RESPONSE_RECEIVED,
            root_cause=REMOTE_SERVER_DROPPED_CONNECTION,
            technical_evidence=f"connection reset mid-request/response: {exc}",
            confidence=CONFIDENCE_HIGH,
            exception_type=type(exc).__name__,
            exception_message=str(exc),
        )

    if isinstance(exc, httpx.ConnectTimeout):
        # httpx's own connect phase bundles DNS+TCP+TLS. If the probe
        # reports a specific stage failure, trust it (it just tested the
        # same host). If the probe says everything succeeded, the original
        # failure could not be reproduced -- CONNECT_TIMEOUT is still the
        # correct category (httpx's own exception type is unambiguous about
        # *which* phase, connect vs. read), just at LOW confidence for the
        # stage attribution specifically.
        if probe is not None and not probe.ok:
            return StageResult(
                last_successful_stage=probe.last_successful_stage,
                failure_stage=probe.failure_stage,
                root_cause=CONNECT_TIMEOUT if probe.root_cause in (
                    DNS_RESOLUTION_FAILED, TCP_CONNECTION_FAILED, TLS_HANDSHAKE_FAILED, CONNECT_TIMEOUT
                ) else probe.root_cause,
                technical_evidence=f"httpx.ConnectTimeout ({exc}); probe confirms: {probe.technical_evidence}",
                confidence=probe.confidence,
                exception_type=type(exc).__name__,
                exception_message=str(exc),
            )
        return StageResult(
            last_successful_stage=(probe.last_successful_stage if probe else None),
            failure_stage=STAGE_TCP_CONNECT,
            root_cause=CONNECT_TIMEOUT,
            technical_evidence=(
                f"original request timed out during httpx's connect phase ({exc}); "
                "a follow-up diagnostic probe against the same host completed successfully "
                "-- likely transient/intermittent, exact stage not reproducible"
            ),
            confidence=CONFIDENCE_LOW,
            exception_type=type(exc).__name__,
            exception_message=str(exc),
        )

    return StageResult(
        last_successful_stage=(probe.last_successful_stage if probe else None),
        failure_stage=STAGE_HTTP_REQUEST_SENT,
        root_cause=UNKNOWN_FAILURE,
        technical_evidence=f"unclassified exception at the HTTP layer: {exc!r}",
        confidence=CONFIDENCE_LOW,
        exception_type=type(exc).__name__ if exc is not None else "",
        exception_message=str(exc) if exc is not None else "",
    )


# ---------------------------------------------------------------------------
# Comparative diagnostics (requirement 5). Never overrides a target's own
# root cause -- only produces an additional, evidence-based conclusion
# string alongside it.
# ---------------------------------------------------------------------------
def build_comparison_conclusion(target: StageResult, control_results: list[StageResult]) -> str:
    """Compares the target's result against control-target results gathered
    in the same run and returns exactly one of the three fixed conclusion
    strings, based only on which of the controls succeeded or failed --
    never a guess, never a relabeling of `target.root_cause`."""
    if not control_results:
        # No controls to compare against -- cannot draw a comparative
        # conclusion at all. Callers should treat this as "unknown", not
        # silently default to one of the three positive conclusions.
        return "No control targets available for comparison"

    controls_failed = [c for c in control_results if not c.ok]
    controls_failed_with_egress_signature = [
        c for c in controls_failed if c.root_cause in EGRESS_PLAUSIBLE_CATEGORIES
    ]

    if controls_failed_with_egress_signature:
        return CONCLUSION_GLOBAL_ISSUE

    if target.ok:
        return CONCLUSION_HEALTHY

    return CONCLUSION_TARGET_SPECIFIC
