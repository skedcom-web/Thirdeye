"""HTTP access layer.

All network egress goes through `HttpFetcher`, which re-checks the official
source policy on the final URL after redirects. Tests inject `OfflineFetcher`
to run the whole pipeline without touching the network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse

from .config import (
    CRAWL_DELAY_SECONDS,
    MAX_DOCUMENT_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
)
from .registry import SourceRejected, assert_approved


class FetchError(RuntimeError):
    def __init__(self, message: str, response: Response | None = None) -> None:
        super().__init__(message)
        self.response = response


@dataclass
class Response:
    url: str  # final URL after redirects
    status_code: int
    content: bytes
    headers: dict[str, str] = field(default_factory=dict)
    response_time_ms: float = 0.0
    duration_ms: float = 0.0
    redirect_count: int = 0
    user_agent: str = ""
    proxy_used: str = ""
    ssl_verified: bool = True
    # `failure_category` holds one of network_diagnosis.ALL_ROOT_CAUSES on
    # failure -- never the literal string "network_failure". `failure_subtype`
    # is kept for backward-compatible callers/templates and mirrors
    # `failure_category` going forward. `last_successful_stage`/
    # `failure_stage`/`confidence_level` are the new Root Cause
    # Classification Framework fields (see operations/network_diagnosis.py).
    failure_category: str = ""
    failure_subtype: str = ""
    error_message: str = ""
    last_successful_stage: str | None = None
    failure_stage: str | None = None
    confidence_level: str = ""
    exception_type: str = ""
    exception_message: str = ""

    @property
    def text(self) -> str:
        charset = "utf-8"
        content_type = self.headers.get("content-type", "")
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";")[0].strip()
        return self.content.decode(charset, errors="replace")

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()


class Fetcher(Protocol):
    def get(self, url: str, *, verify: bool = True, allow_fallback: bool = False, enforce_policy: bool = True) -> Response: ...


class HttpFetcher:
    """Polite, allowlist-enforcing HTTP client with rich diagnostics."""

    def __init__(
        self,
        *,
        delay_seconds: float = CRAWL_DELAY_SECONDS,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        max_bytes: int = MAX_DOCUMENT_BYTES,
    ) -> None:
        self._delay = delay_seconds
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._last_request_at: dict[str, float] = {}
        self._last_url_by_host: dict[str, str] = {}

    def get(self, url: str, *, verify: bool = True, allow_fallback: bool = False, enforce_policy: bool = True) -> Response:
        from . import registry
        from .operations import network_diagnosis as diag

        if enforce_policy:
            try:
                assert_approved(url)
            except registry.SourceRejected as exc:
                dummy = Response(
                    url=url, status_code=0, content=b"",
                    failure_category=diag.POLICY_BLOCKED, failure_subtype=diag.POLICY_BLOCKED,
                    error_message=str(exc),
                    last_successful_stage=diag.STAGE_REQUEST_CREATED,
                    failure_stage=diag.STAGE_POLICY_VALIDATION,
                    confidence_level=diag.CONFIDENCE_HIGH,
                    exception_type=type(exc).__name__, exception_message=str(exc),
                )
                raise FetchError(f"policy rejected: {exc}", dummy) from exc
        self._respect_delay(url)

        import httpx
        import os

        host = urlparse(url).hostname or ""
        scheme = urlparse(url).scheme
        referer = self._last_url_by_host.get(host, f"{scheme}://{host}/")

        proxy = os.environ.get("THIRDEYE_PROXY") or ""
        proxies = {"http://": proxy, "https://": proxy} if proxy else None

        user_agent = USER_AGENT
        headers = {
            "User-Agent": user_agent,
            "Referer": referer,
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ta;q=0.8",
        }

        start_time = time.perf_counter()

        def make_request(current_verify: bool) -> Response:
            # We instantiate a fresh client per request to safely support dynamic proxies,
            # connection scopes, and verify options in concurrent jobs.
            with httpx.Client(
                follow_redirects=True,
                timeout=self._timeout,
                headers=headers,
                verify=current_verify,
                proxy=proxy if proxy else None,
            ) as client:
                req_start = time.perf_counter()
                with client.stream("GET", url) as response:
                    final_url = str(response.url)
                    try:
                        if enforce_policy:
                            assert_approved(final_url)
                    except SourceRejected as exc:
                        redirect_duration = (time.perf_counter() - start_time) * 1000.0
                        dummy = Response(
                            url=final_url, status_code=0, content=b"",
                            duration_ms=redirect_duration, user_agent=user_agent, proxy_used=proxy,
                            failure_category=diag.REDIRECT_POLICY_BLOCKED,
                            failure_subtype=diag.REDIRECT_POLICY_BLOCKED,
                            error_message=str(exc),
                            last_successful_stage=diag.STAGE_HTTP_RESPONSE_RECEIVED,
                            failure_stage=diag.STAGE_REDIRECT_VALIDATION,
                            confidence_level=diag.CONFIDENCE_HIGH,
                            exception_type=type(exc).__name__, exception_message=str(exc),
                        )
                        raise FetchError(f"redirect left approved sources: {exc}", dummy) from exc

                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > self._max_bytes:
                            # Not a network-layer failure (it's our own safety
                            # limit) -- not in the closed root-cause list by
                            # design; UNKNOWN_FAILURE with evidence that says
                            # plainly this is a self-imposed limit, not a
                            # mystery, is more honest than force-fitting it.
                            oversize_duration = (time.perf_counter() - start_time) * 1000.0
                            dummy = Response(
                                url=final_url, status_code=0, content=b"",
                                duration_ms=oversize_duration, user_agent=user_agent, proxy_used=proxy,
                                failure_category=diag.UNKNOWN_FAILURE, failure_subtype=diag.UNKNOWN_FAILURE,
                                error_message=f"response exceeded {self._max_bytes} bytes (self-imposed safety limit, not a network failure)",
                                last_successful_stage=diag.STAGE_HTTP_REQUEST_SENT,
                                failure_stage=diag.STAGE_HTTP_RESPONSE_RECEIVED,
                                confidence_level=diag.CONFIDENCE_HIGH,
                                exception_type="ResponseTooLarge", exception_message=f"exceeded {self._max_bytes} bytes",
                            )
                            raise FetchError(
                                f"response exceeded {self._max_bytes} bytes: {final_url}", dummy
                            )
                        chunks.append(chunk)

                    duration = (time.perf_counter() - req_start) * 1000.0
                    total_duration = (time.perf_counter() - start_time) * 1000.0

                    self._last_url_by_host[host] = final_url

                    # Check for HTTP errors
                    if response.status_code >= 400:
                        raise httpx.HTTPStatusError(
                            f"HTTP {response.status_code}",
                            request=response.request,
                            response=response
                        )

                    return Response(
                        url=final_url,
                        status_code=response.status_code,
                        content=b"".join(chunks),
                        headers={k.lower(): v for k, v in response.headers.items()},
                        response_time_ms=duration,
                        duration_ms=total_duration,
                        redirect_count=len(response.history),
                        user_agent=user_agent,
                        proxy_used=proxy,
                        ssl_verified=current_verify,
                    )

        def _probe(current_verify: bool = True) -> diag.StageResult:
            # Only ever called after httpx has already failed -- the extra
            # round-trip cost is paid solely on the failure path, never on
            # the happy path.
            return diag.probe_connection(
                url, connect_timeout=self._timeout, tls_timeout=self._timeout,
                enforce_policy=enforce_policy,
            )

        try:
            return make_request(current_verify=verify)
        except httpx.ConnectTimeout as exc:
            duration = (time.perf_counter() - start_time) * 1000.0
            result = diag.classify_http_layer_exception(exc, probe=_probe())
            dummy = Response(
                url=url, status_code=0, content=b"", duration_ms=duration,
                user_agent=user_agent, proxy_used=proxy,
                failure_category=result.root_cause, failure_subtype=result.root_cause,
                error_message=result.technical_evidence,
                last_successful_stage=result.last_successful_stage,
                failure_stage=result.failure_stage,
                confidence_level=result.confidence,
                exception_type=result.exception_type, exception_message=result.exception_message,
            )
            raise FetchError(f"Connect timeout: {exc}", dummy) from exc
        except httpx.ReadTimeout as exc:
            duration = (time.perf_counter() - start_time) * 1000.0
            result = diag.classify_http_layer_exception(exc)
            dummy = Response(
                url=url, status_code=0, content=b"", duration_ms=duration,
                user_agent=user_agent, proxy_used=proxy,
                failure_category=result.root_cause, failure_subtype=result.root_cause,
                error_message=result.technical_evidence,
                last_successful_stage=result.last_successful_stage,
                failure_stage=result.failure_stage,
                confidence_level=result.confidence,
                exception_type=result.exception_type, exception_message=result.exception_message,
            )
            raise FetchError(f"Read timeout: {exc}", dummy) from exc
        except httpx.RemoteProtocolError as exc:
            duration = (time.perf_counter() - start_time) * 1000.0
            result = diag.classify_http_layer_exception(exc)
            dummy = Response(
                url=url, status_code=0, content=b"", duration_ms=duration,
                user_agent=user_agent, proxy_used=proxy,
                failure_category=result.root_cause, failure_subtype=result.root_cause,
                error_message=result.technical_evidence,
                last_successful_stage=result.last_successful_stage,
                failure_stage=result.failure_stage,
                confidence_level=result.confidence,
                exception_type=result.exception_type, exception_message=result.exception_message,
            )
            raise FetchError(f"Remote server dropped the connection: {exc}", dummy) from exc
        except httpx.ConnectError as exc:
            # The probe determines the stage definitively (by exception
            # *type*, not string-matching) -- including whether this is a
            # TLS failure, which is what used to be decided by walking
            # __cause__ and matching substrings in the message.
            probe = _probe()
            is_ssl = probe.failure_stage == diag.STAGE_TLS_HANDSHAKE

            if is_ssl and verify and allow_fallback:
                try:
                    res = make_request(current_verify=False)
                    res.ssl_verified = False  # explicitly record that SSL verification was bypassed
                    return res
                except Exception as fallback_exc:
                    duration = (time.perf_counter() - start_time) * 1000.0
                    dummy = Response(
                        url=url, status_code=0, content=b"", duration_ms=duration,
                        user_agent=user_agent, proxy_used=proxy, ssl_verified=False,
                        failure_category=diag.TLS_HANDSHAKE_FAILED, failure_subtype=diag.TLS_HANDSHAKE_FAILED,
                        error_message=f"SSL fallback also failed: {fallback_exc}",
                        last_successful_stage=probe.last_successful_stage,
                        failure_stage=diag.STAGE_TLS_HANDSHAKE,
                        confidence_level=diag.CONFIDENCE_HIGH,
                        exception_type=type(fallback_exc).__name__, exception_message=str(fallback_exc),
                    )
                    raise FetchError(f"SSL fallback failed: {fallback_exc}", dummy) from fallback_exc
            else:
                duration = (time.perf_counter() - start_time) * 1000.0
                root_cause = probe.root_cause or diag.UNKNOWN_FAILURE
                dummy = Response(
                    url=url, status_code=0, content=b"", duration_ms=duration,
                    user_agent=user_agent, proxy_used=proxy, ssl_verified=verify,
                    failure_category=root_cause, failure_subtype=root_cause,
                    error_message=f"{exc} | probe: {probe.technical_evidence}",
                    last_successful_stage=probe.last_successful_stage,
                    failure_stage=probe.failure_stage,
                    confidence_level=probe.confidence,
                    exception_type=probe.exception_type or type(exc).__name__,
                    exception_message=probe.exception_message or str(exc),
                )
                raise FetchError(f"Connection error: {exc}", dummy) from exc
        except httpx.HTTPStatusError as exc:
            duration = (time.perf_counter() - start_time) * 1000.0
            code = exc.response.status_code
            result = diag.classify_http_layer_exception(None, status_code=code)
            dummy = Response(
                url=url, status_code=code, content=b"", duration_ms=duration,
                user_agent=user_agent, proxy_used=proxy,
                failure_category=result.root_cause, failure_subtype=result.root_cause,
                error_message=result.technical_evidence,
                last_successful_stage=result.last_successful_stage,
                failure_stage=result.failure_stage,
                confidence_level=result.confidence,
                exception_type=result.exception_type, exception_message=result.exception_message,
            )
            raise FetchError(f"HTTP {code} status error: {exc}", dummy) from exc
        except FetchError:
            # Already classified inside make_request() (redirect-policy-block,
            # oversize-response limit) -- re-raise as-is instead of letting it
            # fall into the generic handler below, which would discard the
            # existing classification and relabel it UNKNOWN_FAILURE.
            raise
        except Exception as exc:
            duration = (time.perf_counter() - start_time) * 1000.0
            dummy = Response(
                url=url,
                status_code=0,
                content=b"",
                duration_ms=duration,
                user_agent=user_agent,
                proxy_used=proxy,
                failure_category=diag.UNKNOWN_FAILURE,
                failure_subtype=diag.UNKNOWN_FAILURE,
                error_message=str(exc),
                confidence_level=diag.CONFIDENCE_LOW,
                exception_type=type(exc).__name__, exception_message=str(exc),
            )
            raise FetchError(f"Egress failed: {exc!r}", dummy) from exc

    def close(self) -> None:
        pass

    def _respect_delay(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        last = self._last_request_at.get(host)
        now = time.monotonic()
        if last is not None:
            wait = self._delay - (now - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at[host] = time.monotonic()


class OfflineFetcher:
    """Serves canned responses from an in-memory map. Used by the test suite."""

    def __init__(self, responses: dict[str, Response] | None = None) -> None:
        self.responses: dict[str, Response] = responses or {}
        self.requested: list[str] = []

    def add_html(self, url: str, html: str, status: int = 200) -> None:
        self.responses[url] = Response(
            url=url,
            status_code=status,
            content=html.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
            user_agent=USER_AGENT,
        )

    def add_bytes(
        self, url: str, payload: bytes, content_type: str = "application/pdf", status: int = 200
    ) -> None:
        self.responses[url] = Response(
            url=url,
            status_code=status,
            content=payload,
            headers={"content-type": content_type},
            user_agent=USER_AGENT,
        )

    def get(self, url: str, *, verify: bool = True, allow_fallback: bool = False, enforce_policy: bool = True) -> Response:
        from .operations import network_diagnosis as diag

        if enforce_policy:
            try:
                assert_approved(url)
            except SourceRejected as exc:
                dummy = Response(
                    url=url, status_code=0, content=b"",
                    failure_category=diag.POLICY_BLOCKED, failure_subtype=diag.POLICY_BLOCKED,
                    error_message=str(exc),
                    last_successful_stage=diag.STAGE_REQUEST_CREATED,
                    failure_stage=diag.STAGE_POLICY_VALIDATION,
                    confidence_level=diag.CONFIDENCE_HIGH,
                    exception_type=type(exc).__name__, exception_message=str(exc),
                )
                raise FetchError(f"policy rejected: {exc}", dummy) from exc
        self.requested.append(url)
        try:
            res = self.responses[url]
            res.ssl_verified = verify
            return res
        except KeyError:
            # Not a real network failure -- the test harness simply never
            # registered a canned response for this URL.
            dummy = Response(
                url=url,
                status_code=0,
                content=b"",
                user_agent=USER_AGENT,
                failure_category=diag.UNKNOWN_FAILURE,
                failure_subtype=diag.UNKNOWN_FAILURE,
                error_message=f"no offline response registered for {url}",
                confidence_level=diag.CONFIDENCE_LOW,
                exception_type="KeyError", exception_message=f"no offline response registered for {url}",
            )
            raise FetchError(f"no offline response registered for {url}", dummy) from None

    def close(self) -> None:
        pass
