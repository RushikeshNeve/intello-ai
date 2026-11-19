"""Plain-text processor: decode, clean, split into paragraphs with line-range locators."""

from __future__ import annotations

from app.modules.ingestion.chunking import clean_newlines_and_controls
from app.modules.ingestion.contracts import ExtractedDocument, ProcessingError, SourceInfo
from app.modules.ingestion.processors.base import (
    DocumentBuilder,
    Limits,
    check_input_size,
)
from app.modules.ingestion.processors.decoding import charset_from_content_type, decode_bytes

NAME = "text"


def extract_text(
    data: bytes, content_type: str, *, source: SourceInfo, limits: Limits
) -> ExtractedDocument:
    check_input_size(data, limits)
    if not data.strip():
        raise ProcessingError("no_text_extracted", detail="empty input")
    text = decode_bytes(data, hint=charset_from_content_type(content_type))
    lines = clean_newlines_and_controls(text).split("\n")
    builder = DocumentBuilder("text", source, NAME, limits)
    start = 0
    paragraph: list[str] = []

    def flush(end_index: int) -> None:
        if paragraph:
            builder.add("paragraph", "\n".join(paragraph), line_start=start + 1, line_end=end_index)
            paragraph.clear()

    for index, line in enumerate(lines):
        if line.strip():
            if not paragraph:
                start = index
            paragraph.append(line)
        else:
            flush(index)
    flush(len(lines))
    return builder.build()
