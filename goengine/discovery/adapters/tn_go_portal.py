"""Adapter for the Tamil Nadu GO portal search listing (cms.tn.gov.in).

The listing renders one table row per order: GO number, date, department,
abstract, and a link to the PDF. Those cells are captured as *hints* only --
they seed reviewer context and cross-checks, and never substitute for the
values extracted from the PDF itself.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ...registry import is_approved
from .base import DiscoveredLink, PageResult
from .generic_links import GenericLinksAdapter, looks_like_document, normalize

HEADER_HINTS: dict[str, tuple[str, ...]] = {
    "go_number": ("g.o", "go no", "order no", "number", "ms no"),
    "go_date": ("date",),
    "department": ("department", "dept"),
    "subject": ("subject", "abstract", "title", "description"),
}


def _classify_header(text: str) -> str | None:
    lowered = text.strip().lower()
    for field_name, needles in HEADER_HINTS.items():
        if any(needle in lowered for needle in needles):
            return field_name
    return None


class TnGoPortalAdapter:
    name = "tn_go_portal"

    def parse(self, html: str, page_url: str) -> PageResult:
        soup = BeautifulSoup(html, "html.parser")
        documents: list[DiscoveredLink] = []
        seen: set[str] = set()

        for table in soup.find_all("table"):
            columns = self._header_map(table)
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if not cells:
                    continue
                anchors = [
                    a
                    for cell in cells
                    for a in cell.find_all("a", href=True)
                ]
                for anchor in anchors:
                    href = str(anchor["href"]).strip()
                    if not href or href.lower().startswith("javascript:"):
                        continue
                    resolved = normalize(urljoin(page_url, href))
                    text = " ".join(anchor.get_text(" ", strip=True).split())
                    if not is_approved(resolved) or not looks_like_document(resolved, text):
                        continue
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    documents.append(
                        DiscoveredLink(
                            url=resolved,
                            link_text=text,
                            found_on_url=page_url,
                            hints=self._row_hints(cells, columns),
                        )
                    )

        # Rows are the norm, but the portal also renders card layouts for
        # recent orders; fall back so a layout change degrades instead of
        # silently discovering nothing.
        fallback = GenericLinksAdapter().parse(html, page_url)
        for link in fallback.documents:
            if link.url not in seen:
                seen.add(link.url)
                documents.append(link)

        return PageResult(documents=documents, follow=fallback.follow)

    def _header_map(self, table) -> dict[int, str]:
        header_row = table.find("tr")
        if header_row is None:
            return {}
        columns: dict[int, str] = {}
        for index, cell in enumerate(header_row.find_all(["th", "td"])):
            field_name = _classify_header(cell.get_text(" ", strip=True))
            if field_name:
                columns[index] = field_name
        return columns

    def _row_hints(self, cells, columns: dict[int, str]) -> dict[str, str]:
        hints: dict[str, str] = {}
        for index, cell in enumerate(cells):
            field_name = columns.get(index)
            if not field_name:
                continue
            value = " ".join(cell.get_text(" ", strip=True).split())
            if value:
                hints[field_name] = value
        return hints


GO_NUMBER_IN_TEXT = re.compile(r"\bG\.?\s*O\.?\s*(?:Ms|MS|Rt|RT|D)?\.?\s*No\.?\s*(\d+)", re.I)
