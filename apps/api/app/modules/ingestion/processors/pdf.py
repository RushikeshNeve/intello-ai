"""PDF processor (pypdf): text per page with 1-based page locators.

Hard limits: ``Limits.max_pages`` (default 2000), ``Limits.max_bytes`` of input and of extracted
text (default 50 MiB). Encrypted, corrupt or otherwise unparseable files are the permanent error
``pdf_unreadable``; a PDF with no extractable text (a scan without OCR) is ``no_text_extracted``.
pypdf itself bounds decompression of every filter (flate/LZW/run-length/JBIG2), so a decompression
bomb ends in an exception that we map to ``pdf_unreadable``; nothing is rendered, executed or
fetched (JavaScript, launch actions and URI actions are never looked at).

Layout limits (documented, not hidden): reading order is whatever pypdf's text extraction yields, so
multi-column pages, tables and scanned/OCR-less pages come out as plain text lines; blocks are the
blank-line separated runs of a page and never span pages (a sentence broken by a page break becomes
the last block of one page and the first of the next).
"""

from __future__ import annotations

import io
import logging
from typing import Final

from pypdf import PdfReader

from app.modules.ingestion.chunking import clean_newlines_and_controls
from app.modules.ingestion.contracts import ExtractedDocument, ProcessingError, SourceInfo
from app.modules.ingestion.processors.base import DocumentBuilder, Limits, check_input_size

NAME = "pdf"
_HEADER_WINDOW: Final = 1024  # the %PDF- marker must appear this close to the start

# pypdf logs warnings about malformed files; they never contain document text but are noise.
logging.getLogger("pypdf").setLevel(logging.ERROR)


def _unreadable(why: str) -> ProcessingError:
    return ProcessingError("pdf_unreadable", detail=why)


def extract_pdf(
    data: bytes, content_type: str, *, source: SourceInfo, limits: Limits
) -> ExtractedDocument:
    check_input_size(data, limits)
    if not data.strip():
        raise ProcessingError("no_text_extracted", detail="empty input")
    if b"%PDF-" not in data[:_HEADER_WINDOW]:
        raise _unreadable("missing PDF header")
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise _unreadable("encrypted PDF")
        page_count = len(reader.pages)
    except ProcessingError:
        raise
    except Exception as exc:
        raise _unreadable(f"cannot parse: {type(exc).__name__}") from exc
    if page_count > limits.max_pages:
        raise ProcessingError(
            "too_many_pages", detail=f"{page_count} pages exceeds {limits.max_pages}"
        )
    if page_count == 0:
        raise ProcessingError("no_text_extracted", detail="PDF has no pages")

    builder = DocumentBuilder("pdf", source, NAME, limits)
    for index in range(page_count):
        try:
            raw = reader.pages[index].extract_text()
        except Exception as exc:
            raise _unreadable(f"page {index + 1} unreadable: {type(exc).__name__}") from exc
        if len(raw) > limits.max_bytes:
            raise ProcessingError("too_large", detail=f"page {index + 1} text too large")
        cleaned = clean_newlines_and_controls(raw)
        run: list[str] = []
        for line in cleaned.split("\n"):
            if line.strip():
                run.append(line)
            elif run:
                builder.add("paragraph", "\n".join(run), page=index + 1)
                run.clear()
        if run:
            builder.add("paragraph", "\n".join(run), page=index + 1)
    return builder.build(pages=page_count)
