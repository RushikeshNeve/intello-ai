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
from app.modules.ingestion.processors.audio import extract_audio, extract_video
from app.modules.ingestion.processors.base import (
    DEFAULT_LIMITS,
    PROCESSOR_VERSION,
    Limits,
    ProcessorFn,
)
from app.modules.ingestion.processors.code import CODE_CONTENT_TYPES, extract_code
from app.modules.ingestion.processors.csv_xlsx import extract_csv, extract_xlsx
from app.modules.ingestion.processors.html import extract_html
from app.modules.ingestion.processors.markdown import extract_markdown
from app.modules.ingestion.processors.ocr import extract_image
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

_CSV = Processor("csv", PROCESSOR_VERSION, "spreadsheet", extract_csv)
_XLSX = Processor("xlsx", PROCESSOR_VERSION, "spreadsheet", extract_xlsx)
_CODE = Processor("code", PROCESSOR_VERSION, "code", extract_code)
_IMAGE = Processor("ocr", PROCESSOR_VERSION, "image", extract_image)
_AUDIO = Processor("transcript", PROCESSOR_VERSION, "audio", extract_audio)
_VIDEO = Processor("video", PROCESSOR_VERSION, "audio", extract_video)

XLSX_CONTENT_TYPE: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
IMAGE_CONTENT_TYPES: Final = ("image/png", "image/jpeg")
AUDIO_CONTENT_TYPES: Final = (
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/mp4", "audio/x-m4a",
    "audio/ogg", "audio/webm", "audio/flac",
)  # fmt: skip
VIDEO_CONTENT_TYPES: Final = ("video/mp4", "video/webm", "video/quicktime")

_REGISTRY: Final[dict[str, Processor]] = {
    "application/pdf": _PDF,
    "text/plain": _TEXT,
    "text/markdown": _MARKDOWN,
    "text/x-markdown": _MARKDOWN,
    "text/html": _HTML,
    "application/xhtml+xml": _HTML,
    "text/csv": _CSV,
    XLSX_CONTENT_TYPE: _XLSX,
    **dict.fromkeys(CODE_CONTENT_TYPES, _CODE),
    **dict.fromkeys(IMAGE_CONTENT_TYPES, _IMAGE),
    **dict.fromkeys(AUDIO_CONTENT_TYPES, _AUDIO),
    **dict.fromkeys(VIDEO_CONTENT_TYPES, _VIDEO),
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
