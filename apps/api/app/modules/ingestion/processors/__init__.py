"""Pure-function content processors (M03.02.02): bytes in, ``ExtractedDocument`` out."""

from app.modules.ingestion.processors.base import (
    DEFAULT_LIMITS,
    MAX_EXTRACT_BYTES,
    MAX_PDF_PAGES,
    Limits,
)
from app.modules.ingestion.processors.registry import (
    SUPPORTED_CONTENT_TYPES,
    Processor,
    extract,
    get_processor,
    normalize_content_type,
)

__all__ = [
    "DEFAULT_LIMITS",
    "MAX_EXTRACT_BYTES",
    "MAX_PDF_PAGES",
    "SUPPORTED_CONTENT_TYPES",
    "Limits",
    "Processor",
    "extract",
    "get_processor",
    "normalize_content_type",
]
