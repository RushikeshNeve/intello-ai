"""Content type -> processor registry and the single ``extract`` entry point."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.modules.ingestion.contracts import (
    ExtractedDocument,
    Modality,
    ProcessingError,
    SourceInfo,
)
from app.modules.ingestion.processors.base import (
    DEFAULT_LIMITS,
    PROCESSOR_VERSION,
    Limits,
    ProcessorFn,
)
from app.modules.ingestion.processors.html import extract_html
from app.modules.ingestion.processors.markdown import extract_markdown
from app.modules.ingestion.processors.pdf import extract_pdf
from app.modules.ingestion.processors.text import extract_text


@dataclass(frozen=True, slots=True)
class Processor:
    name: str
    version: str
    modality: Modality
    fn: ProcessorFn

    def __call__(
        self,
        data: bytes,
        content_type: str,
        *,
        source: SourceInfo,
        limits: Limits = DEFAULT_LIMITS,
    ) -> ExtractedDocument:
        return self.fn(data, content_type, source=source, limits=limits)


_PDF = Processor("pdf", PROCESSOR_VERSION, "pdf", extract_pdf)
_TEXT = Processor("text", PROCESSOR_VERSION, "text", extract_text)
_MARKDOWN = Processor("markdown", PROCESSOR_VERSION, "markdown", extract_markdown)
_HTML = Processor("html", PROCESSOR_VERSION, "html", extract_html)

_REGISTRY: Final[dict[str, Processor]] = {
    "application/pdf": _PDF,
    "text/plain": _TEXT,
    "text/markdown": _MARKDOWN,
    "text/x-markdown": _MARKDOWN,
    "text/html": _HTML,
    "application/xhtml+xml": _HTML,
}
SUPPORTED_CONTENT_TYPES: Final[frozenset[str]] = frozenset(_REGISTRY)


def normalize_content_type(content_type: str) -> str:
    """The media type without parameters, lower-cased and trimmed."""
    return content_type.partition(";")[0].strip().lower()


def get_processor(content_type: str) -> Processor:
    """The processor for a content type; unknown types are a permanent ``unsupported_type``."""
    processor = _REGISTRY.get(normalize_content_type(content_type))
    if processor is None:
        raise ProcessingError("unsupported_type", permanent=True, detail="no processor for type")
    return processor


def extract(
    data: bytes,
    content_type: str,
    *,
    source: SourceInfo,
    limits: Limits = DEFAULT_LIMITS,
) -> ExtractedDocument:
    """Extract canonical content from raw bytes. Raises only ``ProcessingError``."""
    processor = get_processor(content_type)
    try:
        return processor(data, content_type, source=source, limits=limits)
    except ProcessingError:
        raise
    except Exception as exc:
        raise ProcessingError("malformed_input", detail=f"unexpected {type(exc).__name__}") from exc
