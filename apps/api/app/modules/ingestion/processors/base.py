"""Shared building blocks of the processors: limits, the document builder, section tracking.

Processors are pure functions ``(data, content_type, *, source, limits) -> ExtractedDocument``:
no I/O, no network, no clocks, no randomness. Failures are ``ProcessingError`` with a stable code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Protocol

from app.modules.ingestion.chunking import normalize_block_text
from app.modules.ingestion.contracts import (
    MAX_BLOCKS,
    MAX_HEADING_LEVEL,
    MAX_SECTION_DEPTH,
    MAX_SECTION_TITLE_CHARS,
    STREAM_SEPARATOR,
    Block,
    BlockKind,
    ExtractedDocument,
    Locator,
    Modality,
    ProcessingError,
    ProcessorInfo,
    SourceInfo,
    Stats,
)

PROCESSOR_VERSION: Final = "1"

# Settings-independent defaults (the worker passes its configured values through ``Limits``).
MAX_PDF_PAGES: Final = 2000
MAX_EXTRACT_BYTES: Final = 50 * 1024 * 1024
# bs4 builds one Python object per tag: bound the tag count so memory stays proportional.
MAX_HTML_ELEMENTS: Final = 250_000
MAX_TITLE_CHARS: Final = 300


@dataclass(frozen=True, slots=True)
class Limits:
    """Hard ceilings. ``max_bytes`` bounds both the input size and the extracted text (UTF-8)."""

    max_pages: int = MAX_PDF_PAGES
    max_bytes: int = MAX_EXTRACT_BYTES
    max_blocks: int = MAX_BLOCKS
    max_html_elements: int = MAX_HTML_ELEMENTS

    def __post_init__(self) -> None:
        if min(self.max_pages, self.max_bytes, self.max_blocks, self.max_html_elements) < 1:
            raise ValueError("limits must be positive")
        if self.max_blocks > MAX_BLOCKS:
            raise ValueError("max_blocks exceeds the contract ceiling")


DEFAULT_LIMITS: Final = Limits()


class ProcessorFn(Protocol):
    def __call__(
        self, data: bytes, content_type: str, *, source: SourceInfo, limits: Limits
    ) -> ExtractedDocument: ...


def check_input_size(data: bytes, limits: Limits) -> None:
    if len(data) > limits.max_bytes:
        raise ProcessingError("too_large", detail=f"input larger than {limits.max_bytes} bytes")


class SectionTracker:
    """Heading stack: ``push`` returns the section path *including* the heading just seen."""

    def __init__(self) -> None:
        self._stack: list[tuple[int, str]] = []

    def push(self, level: int, title: str) -> tuple[str, ...]:
        title = " ".join(title.split())[:MAX_TITLE_CHARS][:MAX_SECTION_TITLE_CHARS]
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()
        self._stack.append((level, title))
        if len(self._stack) > MAX_SECTION_DEPTH:
            del self._stack[: len(self._stack) - MAX_SECTION_DEPTH]
        return self.path()

    def path(self) -> tuple[str, ...]:
        return tuple(title for _, title in self._stack)


_LOCATOR_KIND = {
    "pdf": "page",
    "text": "line",
    "markdown": "line",
    "html": "offset",
    "spreadsheet": "cell",
    "code": "line",
    "image": "offset",
    "audio": "time",
}


class DocumentBuilder:
    """Accumulates normalized blocks and computes stream offsets and block ids."""

    def __init__(
        self, modality: Modality, source: SourceInfo, processor_name: str, limits: Limits
    ) -> None:
        self._modality = modality
        self._source = source
        self._name = processor_name
        self._limits = limits
        self._blocks: list[Block] = []
        self._position = 0  # stream offset where the next block starts
        self._bytes = 0

    @property
    def empty(self) -> bool:
        return not self._blocks

    def add(
        self,
        kind: BlockKind,
        text: str,
        *,
        page: int | None = None,
        line_start: int | None = None,
        line_end: int | None = None,
        section_path: tuple[str, ...] = (),
        level: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Normalize ``text`` and append it as a block (dropped when it normalizes to nothing)."""
        text = normalize_block_text(text, kind)
        if not text:
            return
        if len(self._blocks) >= self._limits.max_blocks:
            raise ProcessingError("too_large", detail=f"more than {self._limits.max_blocks} blocks")
        self._bytes += len(text.encode("utf-8")) + (len(STREAM_SEPARATOR) if self._blocks else 0)
        if self._bytes > self._limits.max_bytes:
            raise ProcessingError(
                "too_large", detail=f"extracted text exceeds {self._limits.max_bytes} bytes"
            )
        start = self._position
        end = start + len(text)
        self._blocks.append(
            Block(
                block_id=f"b{len(self._blocks)}",
                kind=kind,
                text=text,
                level=min(level, MAX_HEADING_LEVEL) if level is not None else None,
                locator=Locator(
                    kind=_LOCATOR_KIND[self._modality],  # type: ignore[arg-type]
                    page=page,
                    char_start=start,
                    char_end=end,
                    line_start=line_start,
                    line_end=line_end,
                    section_path=list(section_path) if section_path else None,
                    **(extra or {}),
                ),
            )
        )
        self._position = end + len(STREAM_SEPARATOR)

    def build(
        self,
        *,
        pages: int | None = None,
        processor_version: str = PROCESSOR_VERSION,
        language: str | None = None,
        confidence: float | None = None,
        duration_seconds: float | None = None,
    ) -> ExtractedDocument:
        if not self._blocks:
            raise ProcessingError("no_text_extracted", detail="no extractable text")
        return ExtractedDocument(
            modality=self._modality,
            source=self._source,
            processor=ProcessorInfo(name=self._name, version=processor_version),
            language=language,
            blocks=self._blocks,
            stats=Stats(
                pages=pages,
                chars=self._position - len(STREAM_SEPARATOR),
                confidence=confidence,
                duration_seconds=duration_seconds,
            ),
        )
