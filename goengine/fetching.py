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
    pass


@dataclass
class Response:
    url: str  # final URL after redirects
    status_code: int
    content: bytes
    headers: dict[str, str] = field(default_factory=dict)

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
    def get(self, url: str) -> Response: ...


class HttpFetcher:
    """Polite, allowlist-enforcing HTTP client."""

    def __init__(
        self,
        *,
        delay_seconds: float = CRAWL_DELAY_SECONDS,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        max_bytes: int = MAX_DOCUMENT_BYTES,
    ) -> None:
        import httpx  # imported lazily so offline runs need no network stack

        self._client = httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )
        self._delay = delay_seconds
        self._max_bytes = max_bytes
        self._last_request_at: dict[str, float] = {}

    def get(self, url: str) -> Response:
        assert_approved(url)
        self._respect_delay(url)

        import httpx

        try:
            with self._client.stream("GET", url) as response:
                # Redirects can leave the approved domain; the final URL is
                # what we would actually be archiving, so re-check it.
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

                return Response(
                    url=final_url,
                    status_code=response.status_code,
                    content=b"".join(chunks),
                    headers={k.lower(): v for k, v in response.headers.items()},
                )
        except httpx.HTTPError as exc:
            raise FetchError(f"fetch failed for {url}: {exc}") from exc

    def close(self) -> None:
        self._client.close()

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
        )

    def add_bytes(
        self, url: str, payload: bytes, content_type: str = "application/pdf", status: int = 200
    ) -> None:
        self.responses[url] = Response(
            url=url,
            status_code=status,
            content=payload,
            headers={"content-type": content_type},
        )

    def get(self, url: str) -> Response:
        assert_approved(url)
        self.requested.append(url)
        try:
            return self.responses[url]
        except KeyError:
            raise FetchError(f"no offline response registered for {url}") from None

    def close(self) -> None:
        """No-op: nothing to release. Present so callers can treat every
        Fetcher uniformly (e.g. `finally: fetcher.close()`) without a
        HttpFetcher-specific isinstance check."""
