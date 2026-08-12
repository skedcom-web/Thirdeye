"""Synthetic Government Orders laid out like real TN GOs.

Used by the test suite and by `thirdeye demo` so the pipeline can be exercised
end to end without touching a live government server. These are FIXTURES, not
government data: they never enter a production repository, and the demo runs
against an isolated data directory.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from pathlib import Path

PAGE_WIDTH, PAGE_HEIGHT = 595, 842  # A4 points


@dataclass
class SampleGO:
    go_number: str
    go_date: str  # ISO
    department: str
    subject: str
    body: str
    budget: str | None = None
    district: str | None = None
    scheme_name: str | None = None
    header_department: str = ""
    printed_date: str = ""
    extra_hints: dict[str, str] = field(default_factory=dict)

    def annotation(self, file_name: str) -> dict[str, str]:
        """The ground-truth row for the golden dataset."""
        row = {
            "file_name": file_name,
            "go_number": self.go_number,
            "go_date": self.go_date,
            "department": self.department,
            "subject": self.subject,
        }
        if self.budget:
            row["budget"] = self.budget
        if self.district:
            row["district"] = self.district
        if self.scheme_name:
            row["scheme_name"] = self.scheme_name
        return row


def _page_one_text(sample: SampleGO) -> str:
    header_dept = sample.header_department or sample.department.upper() + " DEPARTMENT"
    lines = [
        "GOVERNMENT OF TAMIL NADU",
        "",
        "ABSTRACT",
        "",
        *textwrap.wrap(sample.subject, width=78),
        "",
        "-" * 78,
        "",
        header_dept,
        "",
        f"{sample.go_number}          Dated: {sample.printed_date}",
        "",
        "Read:",
        "     1. G.O.(Ms) No.11, Finance (BG.I) Department, Dated: 04.01.2024.",
        "     2. Letter No.5567/A1/2025 of the Director, dated 12.02.2025.",
        "",
        "ORDER:",
        "",
    ]
    lines += textwrap.wrap(sample.body, width=78)
    if sample.budget:
        lines += ["", *textwrap.wrap(
            f"2. Sanction is accorded for a sum of {sample.budget} towards the "
            f"implementation of the above proposal during the current financial year.",
            width=78,
        )]
    lines += [
        "",
        "3. This order issues with the concurrence of the Finance Department "
        "vide its U.O. No.1234/FS/P/2026.",
        "",
        "(BY ORDER OF THE GOVERNOR)",
        "",
        "                                             SECRETARY TO GOVERNMENT",
    ]
    return "\n".join(lines)


def _page_two_text(sample: SampleGO) -> str:
    lines = [
        "2",
        "",
        "To",
        "     The Principal Secretary to Government,",
        f"     {sample.department} Department, Chennai - 600 009.",
        "",
        "Copy to:",
        "     The Accountant General (A&E), Chennai - 600 018.",
        "     The Pay and Accounts Officer, Secretariat, Chennai - 600 009.",
        "",
        "                                        // FORWARDED BY ORDER //",
        "",
        "                                             SECTION OFFICER",
    ]
    return "\n".join(lines)


def render_pdf(sample: SampleGO, path: Path) -> Path:
    """Write a two-page PDF with a real text layer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _render_with_pymupdf(sample, path)
    except ImportError:
        _render_minimal_pdf(sample, path)
    return path


def _render_with_pymupdf(sample: SampleGO, path: Path) -> None:
    import pymupdf  # type: ignore

    doc = pymupdf.open()
    for text in (_page_one_text(sample), _page_two_text(sample)):
        page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        page.insert_textbox(
            pymupdf.Rect(56, 56, PAGE_WIDTH - 56, PAGE_HEIGHT - 56),
            text,
            fontsize=9.5,
            fontname="cour",
            align=0,
        )
    doc.save(path)
    doc.close()


def _render_minimal_pdf(sample: SampleGO, path: Path) -> None:
    """Hand-rolled PDF writer, so fixtures work with no PDF library installed."""
    pages_text = [_page_one_text(sample), _page_two_text(sample)]
    objects: list[bytes] = []

    def escape(line: str) -> str:
        return line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    content_ids = []
    page_ids = []
    next_id = 3  # 1 = catalog, 2 = pages tree
    for text in pages_text:
        stream_lines = ["BT", "/F1 9.5 Tf", "11 TL", "56 780 Td"]
        for line in text.split("\n"):
            stream_lines.append(f"({escape(line)}) Tj")
            stream_lines.append("T*")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("latin-1", errors="replace")
        content_id = next_id
        next_id += 1
        page_id = next_id
        next_id += 1
        content_ids.append(content_id)
        page_ids.append(page_id)
        objects.append(
            f"{content_id} 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode()
            + stream
            + b"\nendstream\nendobj\n"
        )
        objects.append(
            (
                f"{page_id} 0 obj\n<< /Type /Page /Parent 2 0 R "
                f"/MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                f"/Resources << /Font << /F1 {next_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>\nendobj\n"
            ).encode()
        )
    font_id = next_id
    objects.append(
        f"{font_id} 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>\nendobj\n".encode()
    )

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    head = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>\nendobj\n".encode(),
    ]

    body = b"%PDF-1.4\n"
    offsets: dict[int, int] = {}
    for chunk in head + objects:
        obj_id = int(chunk.split(b" ", 1)[0])
        offsets[obj_id] = len(body)
        body += chunk

    xref_start = len(body)
    max_id = max(offsets)
    xref = [f"xref\n0 {max_id + 1}\n", "0000000000 65535 f \n"]
    for obj_id in range(1, max_id + 1):
        xref.append(f"{offsets.get(obj_id, 0):010d} 00000 n \n")
    body += "".join(xref).encode()
    body += (
        f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode()
    )
    path.write_bytes(body)


SAMPLES: tuple[SampleGO, ...] = (
    SampleGO(
        go_number="G.O.(Ms) No.123",
        go_date="2026-03-15",
        printed_date="15.03.2026",
        department="Health and Family Welfare",
        header_department="HEALTH AND FAMILY WELFARE (EAP-II) DEPARTMENT",
        subject=(
            "Health and Family Welfare Department - Upgradation of the Primary Health "
            "Centre at Melur into a 30-bedded Community Health Centre in Madurai "
            "District - Administrative sanction - Orders issued."
        ),
        body=(
            "In the letter read above, the Director of Public Health and Preventive "
            "Medicine has requested administrative sanction for the upgradation of the "
            "Primary Health Centre at Melur, Madurai District, into a thirty bedded "
            "Community Health Centre with the required staff and equipment under the "
            "National Health Mission."
        ),
        budget="Rs.2,45,00,000/- (Rupees Two Crore Forty Five Lakh only)",
        district="Madurai",
        scheme_name="National Health Mission",
    ),
    SampleGO(
        go_number="G.O.(Rt) No.456",
        go_date="2026-02-04",
        printed_date="04.02.2026",
        department="School Education",
        header_department="SCHOOL EDUCATION (SE1) DEPARTMENT",
        subject=(
            "School Education Department - Sanction of additional teaching posts for "
            "Government Higher Secondary Schools in Coimbatore District for the "
            "academic year 2026-2027 - Orders issued."
        ),
        body=(
            "The Director of School Education has reported that the sanctioned strength "
            "of teaching staff in certain Government Higher Secondary Schools in "
            "Coimbatore District is inadequate for the enrolment recorded in the current "
            "academic year and has sought sanction for additional posts."
        ),
        budget="Rs.86,40,000/-",
        district="Coimbatore",
    ),
    SampleGO(
        go_number="G.O.(Ms) No.78",
        go_date="2026-01-22",
        printed_date="22.01.2026",
        department="Public Works",
        header_department="PUBLIC WORKS (W2) DEPARTMENT",
        subject=(
            "Public Works Department - Strengthening of the flood protection bund along "
            "the Cooum river in Chennai District under the Integrated Urban Flood "
            "Management Scheme - Administrative sanction - Orders issued."
        ),
        body=(
            "The Engineer-in-Chief, Water Resources Department has submitted proposals "
            "for strengthening the flood protection bund along the Cooum river in "
            "Chennai District to mitigate recurrent inundation during the north-east "
            "monsoon."
        ),
        budget="Rs.12.50 crore",
        district="Chennai",
        scheme_name="Integrated Urban Flood Management Scheme",
    ),
)


def write_samples(directory: Path) -> list[tuple[SampleGO, Path]]:
    """Render the sample set into `directory`. Returns (sample, path) pairs."""
    written: list[tuple[SampleGO, Path]] = []
    for index, sample in enumerate(SAMPLES, start=1):
        digits = "".join(ch for ch in sample.go_number if ch.isdigit()) or str(index)
        year = sample.go_date[:4]
        path = directory / f"GO-{digits}-{year}.pdf"
        render_pdf(sample, path)
        written.append((sample, path))
    return written
