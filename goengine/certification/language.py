"""Module 5 -- Tamil Language Processing Layer.

Classifies a document's script composition as English, Tamil or Mixed before
metadata extraction runs, per the governance rule "language must be
identified before extraction begins". Classification is a Unicode script
count, not a language model: government orders are administrative documents
where script and language coincide closely enough that this is a reliable,
fully offline, zero-dependency signal -- and it is also literally the
diagnostic used to categorize the acquisition dataset (Module 2) and to
break benchmark accuracy down by language (Module 6).
"""

from __future__ import annotations

from dataclasses import dataclass

# The Tamil Unicode block. Deliberately narrow: this counts SCRIPT, not
# language, so Tamil numerals/punctuation elsewhere in the block count too.
TAMIL_BLOCK = (0x0B80, 0x0BFF)

LANGUAGE_ENGLISH = "english"
LANGUAGE_TAMIL = "tamil"
LANGUAGE_MIXED = "mixed"
LANGUAGE_UNKNOWN = "unknown"

# A document at or above this share of one script is called that language
# outright -- TN GOs routinely carry English administrative boilerplate
# (G.O. numbers, department letterheads) even when the operative text is
# entirely in Tamil, and the reverse also happens with Tamil place names in
# otherwise-English orders. Below the threshold on both sides, it's mixed.
DOMINANT_THRESHOLD = 0.85


@dataclass(frozen=True)
class LanguageResult:
    language: str  # english | tamil | mixed | unknown
    tamil_ratio: float
    english_ratio: float
    tamil_chars: int
    english_chars: int
    total_letters: int


def _is_tamil(ch: str) -> bool:
    return TAMIL_BLOCK[0] <= ord(ch) <= TAMIL_BLOCK[1]


def _is_english_letter(ch: str) -> bool:
    return ch.isascii() and ch.isalpha()


def classify_text(text: str, *, dominant_threshold: float = DOMINANT_THRESHOLD) -> LanguageResult:
    tamil_chars = sum(1 for ch in text if _is_tamil(ch))
    english_chars = sum(1 for ch in text if _is_english_letter(ch))
    total = tamil_chars + english_chars

    if total == 0:
        return LanguageResult(LANGUAGE_UNKNOWN, 0.0, 0.0, 0, 0, 0)

    tamil_ratio = tamil_chars / total
    english_ratio = english_chars / total

    if tamil_ratio >= dominant_threshold:
        language = LANGUAGE_TAMIL
    elif english_ratio >= dominant_threshold:
        language = LANGUAGE_ENGLISH
    else:
        language = LANGUAGE_MIXED

    return LanguageResult(
        language=language,
        tamil_ratio=round(tamil_ratio, 4),
        english_ratio=round(english_ratio, 4),
        tamil_chars=tamil_chars,
        english_chars=english_chars,
        total_letters=total,
    )


def classify_pages(pages, *, dominant_threshold: float = DOMINANT_THRESHOLD) -> LanguageResult:
    """Classify a document from its page text (accepts any object with `.text`)."""
    combined = "\n".join(getattr(p, "text", p) for p in pages)
    return classify_text(combined, dominant_threshold=dominant_threshold)
