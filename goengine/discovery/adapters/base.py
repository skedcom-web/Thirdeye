"""Adapter contract for source-specific link discovery.

Portal layouts differ and change. Isolating the HTML-shaped logic in adapters
keeps the crawl loop, dedupe and audit behaviour identical across sources, so
adapting to a redesigned portal is a one-file change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class DiscoveredLink:
    url: str
    link_text: str = ""
    found_on_url: str = ""
    # Adapter-supplied hints (e.g. GO number parsed from the listing row).
    # Hints are never treated as facts: the PDF remains the source of truth.
    hints: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PageResult:
    """What an adapter found on one fetched page."""

    documents: list[DiscoveredLink] = field(default_factory=list)
    # Further listing pages to fetch within the same crawl (pagination).
    follow: list[str] = field(default_factory=list)
    # Granular discovery telemetry properties
    dept_pages_found: int = 0
    go_listings_found: int = 0
    doc_pages_found: int = 0
    doc_links_found: int = 0
    pdf_links_found: int = 0
    rejected_links: int = 0
    skipped_links: int = 0


class Adapter(Protocol):
    name: str

    def parse(self, html: str, page_url: str) -> PageResult:
        """Extract document links and pagination targets from one page."""
        ...
