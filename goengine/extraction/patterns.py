"""Regex patterns and vocabularies for Tamil Nadu Government Orders.

Tuned against the standard TN GO layout:

    GOVERNMENT OF TAMIL NADU
    ABSTRACT
    Health and Family Welfare Department - Upgradation of ... - Orders issued.
    -------------------------------------------------------------------
    HEALTH AND FAMILY WELFARE (EAP-II) DEPARTMENT

    G.O.(Ms) No.123                                Dated: 15.03.2026

Each pattern carries a base precision used as the starting confidence for any
candidate it produces. These are calibrated against the golden dataset -- see
`thirdeye benchmark`.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# GO number
# ---------------------------------------------------------------------------
# G.O.(Ms) No.123 / G.O. Ms. No. 123 / G.O.(Rt) No.456 / G.O.(D) No.78
# [O0] tolerates a very common OCR misread on scanned letterheads: the
# letter O in "G.O." gets read as the digit 0 ("G.0."). Confirmed on a real
# document ("G.0.(D).No.300 Dated: 04.03.2026") where this silently dropped
# the GO number entirely -- the single biggest field the whole app exists
# to extract.
# The number itself uses [0-9] rather than \d: Python's \d matches any
# Unicode decimal digit (category Nd), including Tamil numerals (௦-௯), so
# \d could silently splice a stray Tamil digit from OCR noise into what's
# supposed to be a pure ASCII GO number.
# [\s.,]* (not \s*) between the series and "No.": the same real document has
# OCR text "(D).No.300" with a stray period glued onto the parenthesis --
# plain \s* can't cross that period, so the regex silently skipped the true
# header match entirely and matched a garbled later occurrence instead.
# \s*\.?\s* before the closing bracket, and [({]/[)}] instead of literal
# parens: two more real OCR artifacts found scanning the wider dataset --
# "(Ms.)" (a stray period before the paren closes) and "G.0.{Ms.)" (OCR
# reads "(" as the visually similar "{"). Both silently dropped the series
# match, which then failed the whole GO number for that document.
GO_NUMBER_FULL = re.compile(
    r"""
    \bG\.?\s*[O0]\.?\s*                  # G.O. (or G.0. -- OCR O/0 confusion)
    (?:[({]\s*(?P<series>Ms|MS|Rt|RT|D|P|2D)\s*\.?\s*[)}]|(?P<series2>Ms|MS|Rt|RT|D|P)\s*\.)?  # (Ms) / {Ms.) / Ms.
    [\s.,]*(?:No\.?|Number)?\s*
    (?P<number>[0-9]{1,5})
    (?:\s*[/,]?\s*(?P<year>(?:19|20)[0-9]{2}))?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Bare "Order No. 123" -- weaker, used only when nothing better is found.
ORDER_NUMBER_LOOSE = re.compile(
    r"\b(?:Order|Proceedings)\s+No\.?\s*(?P<number>[0-9]{1,5})", re.IGNORECASE
)

GO_SERIES_LABELS = {"MS": "Ms", "RT": "Rt", "D": "D", "P": "P", "2D": "2D"}

# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

# Dated: 15.03.2026 / Dated 15-03-2026 / Dated: 15/03/2026
# [0-9] everywhere below, not \d, for the same Tamil-numeral reason as
# GO_NUMBER_FULL above.
DATE_NUMERIC_LABELLED = re.compile(
    r"\b(?:Dated?|Date)\s*[:\-]?\s*(?P<day>[0-9]{1,2})\s*[./-]\s*(?P<month>[0-9]{1,2})\s*[./-]\s*(?P<year>[0-9]{2,4})",
    re.IGNORECASE,
)

# Dated: 15th March 2026 / Dated 15 March, 2026
DATE_TEXTUAL_LABELLED = re.compile(
    r"\b(?:Dated?|Date)\s*[:\-]?\s*(?P<day>[0-9]{1,2})\s*(?:st|nd|rd|th)?\s*"
    r"(?P<month>" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\s*,?\s*(?P<year>[0-9]{4})",
    re.IGNORECASE,
)

# Unlabelled fallbacks, lower precision.
DATE_NUMERIC_BARE = re.compile(
    r"\b(?P<day>[0-9]{1,2})\s*[./-]\s*(?P<month>[0-9]{1,2})\s*[./-]\s*(?P<year>(?:19|20)[0-9]{2})\b"
)
DATE_TEXTUAL_BARE = re.compile(
    r"\b(?P<day>[0-9]{1,2})\s*(?:st|nd|rd|th)?\s+"
    r"(?P<month>" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\s*,?\s*(?P<year>[0-9]{4})\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Department
# ---------------------------------------------------------------------------
# A standalone all-caps line ending in DEPARTMENT, optionally with a section
# code: "HEALTH AND FAMILY WELFARE (EAP-II) DEPARTMENT"
# The section-code group uses [^()\n] (any non-paren text) rather than
# [A-Z0-9\-.\s]: OCR frequently mangles that code into mixed case and stray
# replacement characters ("(IMi�2)" for what should read "(IM I-2)"),
# which the tighter class couldn't match at all -- and since the code itself
# is never captured or used, being permissive here carries no real risk.
DEPARTMENT_HEADING = re.compile(
    r"^(?P<name>[A-Z][A-Z&.,'()\-/ ]{3,80}?)\s*(?:\([^()\n]{1,20}\)\s*)?DEPARTMENT\s*$",
    re.MULTILINE,
)

# Title-case form inside the abstract: "Health and Family Welfare Department -"
# The optional section-code group mirrors DEPARTMENT_HEADING above: a real
# document had "Health and Family Welfare (EAP.II/1) Department" -- without
# tolerating that parenthetical, \s+Department\b right after the name never
# matched at all.
DEPARTMENT_IN_ABSTRACT = re.compile(
    r"(?P<name>[A-Z][A-Za-z&.,'()\-/ ]{3,80}?)\s+(?:\([^()\n]{1,20}\)\s+)?Department\b"
)

# Canonical TN secretariat departments, used to normalize and to score.
KNOWN_DEPARTMENTS: tuple[str, ...] = (
    "Adi Dravidar and Tribal Welfare",
    "Agriculture and Farmers Welfare",
    "Animal Husbandry, Dairying and Fisheries",
    "Backward Classes, Most Backward Classes and Minorities Welfare",
    "Commercial Taxes and Registration",
    "Co-operation, Food and Consumer Protection",
    "Energy",
    "Environment, Climate Change and Forests",
    "Finance",
    "Handlooms, Handicrafts, Textiles and Khadi",
    "Health and Family Welfare",
    "Higher Education",
    "Highways and Minor Ports",
    "Home, Prohibition and Excise",
    "Housing and Urban Development",
    "Industries, Investment Promotion and Commerce",
    "Information Technology and Digital Services",
    "Labour Welfare and Skill Development",
    "Law",
    "Micro, Small and Medium Enterprises",
    "Municipal Administration and Water Supply",
    "Personnel and Administrative Reforms",
    "Planning, Development and Special Initiatives",
    "Public",
    "Public Works",
    "Revenue and Disaster Management",
    "Rural Development and Panchayat Raj",
    "School Education",
    "Social Welfare and Women Empowerment",
    "Tamil Development and Information",
    "Tourism, Culture and Religious Endowments",
    "Transport",
    "Water Resources",
    "Youth Welfare and Sports Development",
)

# ---------------------------------------------------------------------------
# Subject / abstract
# ---------------------------------------------------------------------------
# [O0] in the stop-lookahead tolerates the same OCR O/0 confusion as
# GO_NUMBER_FULL: without it, an abstract followed by a "G.0." (not "G.O.")
# header line never found its stop point and swallowed the department/GO
# number/date lines whole, confirmed on a real document. The all-caps
# "...DEPARTMENT" line alternative stops there too: several real GOs have no
# dashed separator between the abstract and the department heading, so
# without it the body ran on to include the department name itself. That
# alternative is wrapped in (?-i:...) to force case-sensitivity locally --
# the overall pattern is IGNORECASE for "ABSTRACT"/"READ", but an
# all-lowercase match here would also fire mid-sentence on ordinary
# Title-Case abstract text like "...Welfare Department - Upgradation...".
ABSTRACT_BLOCK = re.compile(
    r"^\s*ABSTRACT\s*\n(?P<body>.+?)"
    r"(?=\n\s*(?:-{4,}|_{4,}|={4,}|(?-i:[A-Z][A-Z0-9&.,'()\-/ ]{3,80}?DEPARTMENT)\s*$|G\.?\s*[O0]\.?[\s.(]|READ\b|Read\b))",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
# Fallback: the abstract usually closes with some form of "Orders issued".
ORDERS_ISSUED_TAIL = re.compile(r"Orders?\s*[-–—]?\s*issued\.?", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Optional Phase 1 fields
# ---------------------------------------------------------------------------
# Rs.12,50,000/- | Rs. 1.25 crore | Rupees Twelve Lakh
MONEY_NUMERIC = re.compile(
    r"(?:Rs\.?|INR|₹|Rupees)\s*(?P<amount>[0-9][0-9,]*(?:\.[0-9]+)?)\s*"
    r"(?P<unit>crores?|lakhs?|lacs?|thousand)?\s*(?:/-)?",
    re.IGNORECASE,
)
UNIT_MULTIPLIERS = {
    "crore": 10_000_000, "crores": 10_000_000,
    "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
    "thousand": 1_000,
}

TN_DISTRICTS: tuple[str, ...] = (
    "Ariyalur", "Chengalpattu", "Chennai", "Coimbatore", "Cuddalore", "Dharmapuri",
    "Dindigul", "Erode", "Kallakurichi", "Kancheepuram", "Kanniyakumari", "Karur",
    "Krishnagiri", "Madurai", "Mayiladuthurai", "Nagapattinam", "Namakkal",
    "Nilgiris", "Perambalur", "Pudukkottai", "Ramanathapuram", "Ranipet",
    "Salem", "Sivaganga", "Tenkasi", "Thanjavur", "Theni", "Thoothukudi",
    "Tiruchirappalli", "Tirunelveli", "Tirupathur", "Tiruppur", "Tiruvallur",
    "Tiruvannamalai", "Tiruvarur", "Vellore", "Viluppuram", "Virudhunagar",
)
# Spellings that appear in older orders.
DISTRICT_ALIASES: dict[str, str] = {
    "kanyakumari": "Kanniyakumari",
    "tuticorin": "Thoothukudi",
    "trichy": "Tiruchirappalli",
    "tiruchirapalli": "Tiruchirappalli",
    "the nilgiris": "Nilgiris",
    "villupuram": "Viluppuram",
    "vilupuram": "Viluppuram",
    "tirupur": "Tiruppur",
    "sivagangai": "Sivaganga",
    "tiruvanamalai": "Tiruvannamalai",
}

# Either a quoted name, or up to six Title-Case words running into a scheme
# keyword. Only "of/and/for/the" may appear lowercase inside the name, which
# stops the match from swallowing the preceding clause ("... District under
# the Integrated Flood Scheme").
SCHEME_NAME = re.compile(
    r"(?:[\"'“‘])(?P<name>[A-Z][^\"'”’\n]{5,90}?)(?:[\"'”’])"
    r"|\b(?P<name2>(?:[A-Z][A-Za-z'’.\-]*|of|and|for|the)"
    r"(?:\s+(?:[A-Z][A-Za-z'’.\-]*|of|and|for|the)){0,5}"
    r"\s+(?:Scheme|Thittam|Mission|Yojana|Programme|Project))\b"
)

# Structural markers. A GO cites the orders it was issued under in a "Read:"
# block; numbers and dates in that block belong to OTHER orders and must not
# outrank the header. "ORDER:" opens the operative text.
READ_MARKER = re.compile(r"^[ \t]*Read\s*[:\-]", re.IGNORECASE | re.MULTILINE)
ORDER_MARKER = re.compile(r"^[ \t]*ORDER\s*[:\-]", re.MULTILINE)

# Leading filler to strip off a scheme name once matched.
SCHEME_LEADING_FILLER = re.compile(r"^(?:the|under|of|and|for)\s+", re.IGNORECASE)


def normalize_department(raw: str) -> str:
    """Map a heading to a canonical department name where possible."""
    cleaned = re.sub(r"\s+", " ", raw).strip(" -–—,.")
    cleaned = re.sub(r"\(.*?\)", " ", cleaned)  # drop section codes like (EAP-II)
    cleaned = re.sub(r"\bDEPARTMENT\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—,.")
    if not cleaned:
        return ""

    folded = _fold(cleaned)
    for known in KNOWN_DEPARTMENTS:
        if _fold(known) == folded:
            return known
    for known in KNOWN_DEPARTMENTS:
        known_folded = _fold(known)
        if folded.startswith(known_folded) or known_folded.startswith(folded):
            return known
    return cleaned.title() if cleaned.isupper() else cleaned


def is_known_department(name: str) -> bool:
    return any(_fold(name) == _fold(known) for known in KNOWN_DEPARTMENTS)


def normalize_district(raw: str) -> str | None:
    folded = _fold(raw)
    alias = DISTRICT_ALIASES.get(folded)
    if alias:
        return alias
    for district in TN_DISTRICTS:
        if _fold(district) == folded:
            return district
    return None


def _fold(value: str) -> str:
    value = value.lower().replace("&", "and")
    value = re.sub(r"[^a-z0-9 ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()
