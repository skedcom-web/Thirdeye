"""Modules 5 & 6 -- text and metadata extraction."""

from .metadata import (
    ALL_FIELDS,
    CORE_FIELDS,
    EXTRACTOR_VERSION,
    OPTIONAL_FIELDS,
    ExtractedMetadata,
    FieldCandidate,
    extract_and_store,
    extract_metadata,
    load_fields,
)
from .text import (
    ExtractionError,
    ExtractionOutput,
    PageText,
    extract_document,
    extract_file,
    load_pages,
)

__all__ = [
    "ALL_FIELDS",
    "CORE_FIELDS",
    "OPTIONAL_FIELDS",
    "EXTRACTOR_VERSION",
    "ExtractedMetadata",
    "ExtractionError",
    "ExtractionOutput",
    "FieldCandidate",
    "PageText",
    "extract_and_store",
    "extract_document",
    "extract_file",
    "extract_metadata",
    "load_fields",
    "load_pages",
]
