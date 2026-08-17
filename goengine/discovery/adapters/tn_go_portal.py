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

# Directory and per-department listing pages on tn.gov.in-style portals.
# These carry no PDF in their own URL (so `looks_like_document` never
# matches them), but they are exactly the pages a department directory
# links to and that a department listing page paginates into -- without
# recognizing them, a crawl that starts on a hub page like `godept_list.php`
# never reaches the tables that actually contain the GO links.
LISTING_PAGE_RE = re.compile(
    r"(?:godept_list|document_dept_list|go\.php|whatsnew\.php)", re.IGNORECASE
)

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

        dept_pages_found = 0
        go_listings_found = 0
        doc_pages_found = 0
        doc_links_found = 0
        pdf_links_found = 0
        rejected_links = 0
        skipped_links = 0

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
                    
                    if not is_approved(resolved):
                        rejected_links += 1
                        continue
                    if not looks_like_document(resolved, text):
                        if "dep_id=" in resolved or "godept_list.php" in resolved or "department" in resolved.lower():
                            dept_pages_found += 1
                        elif "go.php" in resolved or "go-search" in resolved or "gazette" in resolved.lower():
                            go_listings_found += 1
                        else:
                            doc_pages_found += 1
                        continue
                        
                    if resolved in seen:
                        skipped_links += 1
                        continue
                    seen.add(resolved)
                    
                    doc_links_found += 1
                    if resolved.lower().endswith(".pdf") or ".pdf?" in resolved.lower():
                        pdf_links_found += 1
                        
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
                doc_links_found += 1
                if link.url.lower().endswith(".pdf") or ".pdf?" in link.url.lower():
                    pdf_links_found += 1

        dept_pages_found += fallback.dept_pages_found
        go_listings_found += fallback.go_listings_found
        doc_pages_found += fallback.doc_pages_found
        rejected_links += fallback.rejected_links
        skipped_links += fallback.skipped_links

        follow = list(fallback.follow)
        follow_seen = set(follow)
        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"]).strip()
            if not href or href.lower().startswith("javascript:"):
                continue
            resolved = normalize(urljoin(page_url, href))
            if (
                resolved not in seen
                and resolved not in follow_seen
                and resolved != page_url
                and is_approved(resolved)
                and LISTING_PAGE_RE.search(resolved)
            ):
                follow_seen.add(resolved)
                follow.append(resolved)
                go_listings_found += 1

        return PageResult(
            documents=documents,
            follow=follow,
            dept_pages_found=dept_pages_found,
            go_listings_found=go_listings_found,
            doc_pages_found=doc_pages_found,
            doc_links_found=doc_links_found,
            pdf_links_found=pdf_links_found,
            rejected_links=rejected_links,
            skipped_links=skipped_links,
        )

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
