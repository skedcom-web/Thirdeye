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
    failure_category: str = ""
    failure_subtype: str = ""
    error_message: str = ""

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
        if enforce_policy:
            assert_approved(url)
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
                        assert_approved(final_url)
                    except SourceRejected as exc:
                        raise FetchError(f"redirect left approved sources: {exc}") from exc

                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > self._max_bytes:
                            raise FetchError(
                                f"response exceeded {self._max_bytes} bytes: {final_url}"
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

        try:
            return make_request(current_verify=verify)
        except httpx.ConnectTimeout as exc:
            duration = (time.perf_counter() - start_time) * 1000.0
            dummy = Response(
                url=url,
                status_code=0,
                content=b"",
                duration_ms=duration,
                user_agent=user_agent,
                proxy_used=proxy,
                failure_category="network_failure",
                failure_subtype="timeout",
                error_message=str(exc),
            )
            raise FetchError(f"Connection timeout: {exc}", dummy) from exc
        except httpx.ReadTimeout as exc:
            duration = (time.perf_counter() - start_time) * 1000.0
            dummy = Response(
                url=url,
                status_code=0,
                content=b"",
                duration_ms=duration,
                user_agent=user_agent,
                proxy_used=proxy,
                failure_category="network_failure",
                failure_subtype="timeout",
                error_message=str(exc),
            )
            raise FetchError(f"Read timeout: {exc}", dummy) from exc
        except httpx.ConnectError as exc:
            import ssl
            is_ssl = False
            cause = exc
            while cause:
                if isinstance(cause, ssl.SSLError):
                    is_ssl = True
                    break
                cause = cause.__cause__ or (cause.args[0] if cause.args and isinstance(cause.args[0], Exception) else None)
            if not is_ssl and any(word in str(exc).lower() for word in ("ssl", "cert", "handshake")):
                is_ssl = True

            if is_ssl and verify and allow_fallback:
                # SSL Fallback retry
                try:
                    res = make_request(current_verify=False)
                    res.ssl_verified = False  # explicitly record that SSL verification was bypassed
                    return res
                except Exception as fallback_exc:
                    duration = (time.perf_counter() - start_time) * 1000.0
                    dummy = Response(
                        url=url,
                        status_code=0,
                        content=b"",
                        duration_ms=duration,
                        user_agent=user_agent,
                        proxy_used=proxy,
                        ssl_verified=False,
                        failure_category="network_failure",
                        failure_subtype="ssl_error",
                        error_message=f"SSL Fallback failed: {fallback_exc}",
                    )
                    raise FetchError(f"SSL Fallback failed: {fallback_exc}", dummy) from fallback_exc
            elif is_ssl:
                duration = (time.perf_counter() - start_time) * 1000.0
                dummy = Response(
                    url=url,
                    status_code=0,
                    content=b"",
                    duration_ms=duration,
                    user_agent=user_agent,
                    proxy_used=proxy,
                    ssl_verified=verify,
                    failure_category="network_failure",
                    failure_subtype="ssl_error",
                    error_message=str(exc),
                )
                raise FetchError(f"SSL verification failed: {exc}", dummy) from exc
            else:
                duration = (time.perf_counter() - start_time) * 1000.0
                subtype = "dns" if "getaddrinfo" in str(exc) else "connection_refused"
                dummy = Response(
                    url=url,
                    status_code=0,
                    content=b"",
                    duration_ms=duration,
                    user_agent=user_agent,
                    proxy_used=proxy,
                    failure_category="network_failure",
                    failure_subtype=subtype,
                    error_message=str(exc),
                )
                raise FetchError(f"Connection error: {exc}", dummy) from exc
        except httpx.HTTPStatusError as exc:
            duration = (time.perf_counter() - start_time) * 1000.0
            code = exc.response.status_code
            dummy = Response(
                url=url,
                status_code=code,
                content=b"",
                duration_ms=duration,
                user_agent=user_agent,
                proxy_used=proxy,
                failure_category="network_failure",
                failure_subtype=f"http_{code}",
                error_message=str(exc),
            )
            raise FetchError(f"HTTP {code} status error: {exc}", dummy) from exc
        except Exception as exc:
            duration = (time.perf_counter() - start_time) * 1000.0
            dummy = Response(
                url=url,
                status_code=0,
                content=b"",
                duration_ms=duration,
                user_agent=user_agent,
                proxy_used=proxy,
                failure_category="network_failure",
                failure_subtype="unknown_network",
                error_message=str(exc),
            )
            raise FetchError(f"Egress failed: {exc}", dummy) from exc

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
        if enforce_policy:
            assert_approved(url)
        self.requested.append(url)
        try:
            res = self.responses[url]
            res.ssl_verified = verify
            return res
        except KeyError:
            dummy = Response(
                url=url,
                status_code=0,
                content=b"",
                user_agent=USER_AGENT,
                failure_category="network_failure",
                failure_subtype="http_404",
                error_message=f"no offline response registered for {url}",
            )
            raise FetchError(f"no offline response registered for {url}", dummy) from None

    def close(self) -> None:
        pass
