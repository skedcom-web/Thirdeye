"""Generic PDF link harvester.

Works on most government listing pages: collect every anchor that resolves to a
PDF on an approved host, plus obvious pagination links. Sources with a richer
structure get a dedicated adapter.
"""

from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

from ...registry import is_approved
from .base import DiscoveredLink, PageResult

PDF_SUFFIX_RE = re.compile(r"\.pdf(?:$|[?#])", re.IGNORECASE)
# Portals often serve PDFs through a handler rather than a .pdf path.
PDF_HANDLER_RE = re.compile(
    r"(?:download|viewfile|getfile|fileview|attachment|/go/|documents?)", re.IGNORECASE
)
PAGINATION_RE = re.compile(r"(?:[?&](?:page|pageno|start|offset)=\d+)", re.IGNORECASE)
NEXT_TEXT_RE = re.compile(r"^\s*(?:next|»|>>|more|\d+)\s*$", re.IGNORECASE)


def looks_like_document(url: str, link_text: str = "") -> bool:
    if PDF_SUFFIX_RE.search(url):
        return True
    # A handler URL only counts when the link text also suggests an order,
    # otherwise every nav item on the page qualifies.
    if PDF_HANDLER_RE.search(url) and re.search(
        r"\b(?:g\.?o\.?|order|ms\.?\s*no|நிர்வாக|ஆணை)\b", link_text, re.IGNORECASE
    ):
        return True
    return False


def normalize(url: str) -> str:
    """Drop fragments so #page-anchors don't create duplicate documents."""
    return urldefrag(url)[0].strip()


class GenericLinksAdapter:
    name = "generic_links"

    def parse(self, html: str, page_url: str) -> PageResult:
        soup = BeautifulSoup(html, "html.parser")
        documents: list[DiscoveredLink] = []
        follow: list[str] = []
        seen: set[str] = set()

        base_host = urlparse(page_url).hostname or ""

        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"]).strip()
            if not href or href.lower().startswith(("javascript:", "mailto:", "tel:")):
                continue
            resolved = normalize(urljoin(page_url, href))
            if resolved in seen:
                continue
            seen.add(resolved)

            text = " ".join(anchor.get_text(" ", strip=True).split())

            if not is_approved(resolved):
                continue

            if looks_like_document(resolved, text):
                documents.append(
                    DiscoveredLink(url=resolved, link_text=text, found_on_url=page_url)
                )
            elif PAGINATION_RE.search(resolved) or (
                NEXT_TEXT_RE.match(text) and (urlparse(resolved).hostname or "") == base_host
            ):
                follow.append(resolved)

        return PageResult(documents=documents, follow=follow)
