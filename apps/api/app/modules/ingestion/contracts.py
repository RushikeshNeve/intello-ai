"""Canonical extracted-content contract of the ingestion pipeline (M03 contract §7, M03.02.01).

``ExtractedDocument`` is the JSON artifact (``extracted.json``) every processor produces and the
chunker consumes. Everything here is pure data + validation; no I/O.

Offsets. ``Locator.char_start``/``char_end`` are *character* (code point) offsets into the
document's **block text stream**: all block texts joined by two newlines. Block ``i`` therefore
occupies ``stream[char_start:char_end]`` and the next block starts two characters after
``char_end``. The model validator below enforces this exactly, so a document that validates can
always be mapped back from a chunk to a page, a heading path and exact offsets.

Canonical JSON. ``to_canonical_json()`` is sorted-keys, no whitespace, UTF-8 (no backslash-u
escapes), ``None`` fields omitted, NaN rejected. Only str/int/bool/list/dict values exist in these
models, so the bytes are identical across runs and Python versions; ``sha256_hex()`` of them is
what checkpoints fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Annotated, Any, ClassVar, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

SCHEMA_VERSION: Final = 1

# Hard ceilings enforced by the validators (processors apply tighter, configurable ``Limits``).
MAX_BLOCKS: Final = 500_000
MAX_STREAM_CHARS: Final = 50 * 1024 * 1024
MAX_SECTION_DEPTH: Final = 32
MAX_SECTION_TITLE_CHARS: Final = 500
MAX_HEADING_LEVEL: Final = 6
MAX_CHUNK_LOCATORS: Final = MAX_BLOCKS

Modality = Literal["pdf", "text", "markdown", "html"]
BlockKind = Literal["heading", "paragraph", "list", "table", "code", "quote"]
LocatorKind = Literal["page", "offset", "line"]
ErrorCode = Literal[
    "unsupported_type",
    "no_text_extracted",
    "pdf_unreadable",
    "encoding_unreadable",
    "too_large",
    "too_many_pages",
    "malformed_input",
]
ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "unsupported_type",
        "no_text_extracted",
        "pdf_unreadable",
        "encoding_unreadable",
        "too_large",
        "too_many_pages",
        "malformed_input",
    }
)
MAX_ERROR_DETAIL_CHARS: Final = 200

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ProcessingError(Exception):
    """A stable, closed-set failure of extraction or chunking.

    ``detail`` is for operators: it must never contain document text (callers pass a fixed phrase or
    counts). It is truncated and newlines are replaced so it is safe to store and log.
    """

    def __init__(self, code: str, *, permanent: bool = True, detail: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown processing error code: {code!r}")
        self.code: ErrorCode = code  # type: ignore[assignment]  # validated against the closed set
        self.permanent = permanent
        self.detail = " ".join(detail.split())[:MAX_ERROR_DETAIL_CHARS]
        super().__init__(f"{code}: {self.detail}" if self.detail else code)

    def to_dict(self) -> dict[str, str | bool]:
        return {"code": self.code, "permanent": self.permanent, "detail": self.detail}


def _utf8_safe(value: str) -> str:
    """Reject lone surrogates: they cannot be encoded to the UTF-8 canonical JSON."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("text contains characters that cannot be encoded as UTF-8") from exc
    return value


class _Model(BaseModel):
    """Strict, immutable base for every contract model."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def to_canonical_json(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json", exclude_none=True))

    def sha256_hex(self) -> str:
        return hashlib.sha256(self.to_canonical_json()).hexdigest()

    @classmethod
    def from_json(cls, data: bytes | str) -> Self:
        """Strictly parse and validate JSON (unknown fields, wrong types, bad invariants fail)."""
        return cls.model_validate_json(data, strict=True)


def canonical_json_bytes(obj: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8 (no ``\\u`` escapes), NaN/Infinity rejected."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


class SourceInfo(_Model):
    """The document version the content was extracted from (as recorded by M02)."""

    version_id: uuid.UUID
    sha256: Sha256Hex
    content_type: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    byte_size: Annotated[int, Field(ge=0)]


class ProcessorInfo(_Model):
    name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    version: Annotated[str, StringConstraints(min_length=1, max_length=32)]


class Stats(_Model):
    pages: Annotated[int, Field(ge=0)] | None = None
    chars: Annotated[int, Field(ge=0, le=MAX_STREAM_CHARS)]


class Locator(_Model):
    """Where a block (or the part of a block a chunk covers) came from.

    ``page`` is 1-based and present for ``kind == "page"``; ``line_start``/``line_end`` are
    1-based, inclusive, in the decoded source text (present for ``kind == "line"``);
    ``char_start``/``char_end`` are offsets into the normalized block text stream (see the module
    docstring), end exclusive.
    """

    kind: LocatorKind
    page: Annotated[int, Field(ge=1)] | None = None
    char_start: Annotated[int, Field(ge=0, le=MAX_STREAM_CHARS)]
    char_end: Annotated[int, Field(ge=0, le=MAX_STREAM_CHARS)]
    line_start: Annotated[int, Field(ge=1)] | None = None
    line_end: Annotated[int, Field(ge=1)] | None = None
    section_path: (
        Annotated[
            list[Annotated[str, StringConstraints(max_length=MAX_SECTION_TITLE_CHARS)]],
            Field(max_length=MAX_SECTION_DEPTH),
        ]
        | None
    ) = None

    @field_validator("section_path")
    @classmethod
    def _section_utf8(cls, value: list[str] | None) -> list[str] | None:
        for title in value or []:
            _utf8_safe(title)
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.char_end < self.char_start:
            raise ValueError("char_end must be >= char_start")
        if self.kind == "page" and self.page is None:
            raise ValueError("a page locator needs a page number")
        if self.kind != "page" and self.page is not None:
            raise ValueError("only page locators carry a page number")
        if (self.line_start is None) != (self.line_end is None):
            raise ValueError("line_start and line_end go together")
        if self.kind == "line" and self.line_start is None:
            raise ValueError("a line locator needs a line range")
        if self.line_start is not None and self.line_end is not None:
            if self.kind != "line":
                raise ValueError("only line locators carry a line range")
            if self.line_end < self.line_start:
                raise ValueError("line_end must be >= line_start")
        return self


class Block(_Model):
    block_id: Annotated[str, StringConstraints(pattern=r"^b(0|[1-9][0-9]{0,6})$")]
    kind: BlockKind
    text: Annotated[str, StringConstraints(min_length=1, max_length=MAX_STREAM_CHARS)]
    level: Annotated[int, Field(ge=1, le=MAX_HEADING_LEVEL)] | None = None
    locator: Locator

    @field_validator("text")
    @classmethod
    def _text_utf8(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a block must contain non-whitespace text")
        return _utf8_safe(value)

    @model_validator(mode="after")
    def _level_only_for_headings(self) -> Self:
        if self.kind == "heading" and self.level is None:
            raise ValueError("a heading needs a level")
        if self.kind != "heading" and self.level is not None:
            raise ValueError("only headings carry a level")
        if self.locator.char_end - self.locator.char_start != len(self.text):
            raise ValueError("locator span must equal the block text length")
        return self


STREAM_SEPARATOR: Final = "\n\n"


class ExtractedDocument(_Model):
    schema_version: Literal[1] = SCHEMA_VERSION
    modality: Modality
    source: SourceInfo
    processor: ProcessorInfo
    language: Annotated[str, StringConstraints(min_length=2, max_length=35)] | None = None
    blocks: Annotated[list[Block], Field(max_length=MAX_BLOCKS)]
    stats: Stats

    EXPECTED_LOCATOR: ClassVar[dict[str, str]] = {
        "pdf": "page",
        "text": "line",
        "markdown": "line",
        "html": "offset",
    }

    @model_validator(mode="after")
    def _blocks_consistent(self) -> Self:
        expected_kind = self.EXPECTED_LOCATOR[self.modality]
        position = 0
        for index, block in enumerate(self.blocks):
            if block.block_id != f"b{index}":
                raise ValueError("block ids must be b0, b1, ... in order")
            loc = block.locator
            if loc.kind != expected_kind:
                raise ValueError(f"a {self.modality} document uses {expected_kind} locators")
            if loc.char_start != position:
                raise ValueError("block offsets must follow the \\n\\n-joined text stream")
            position = loc.char_end + len(STREAM_SEPARATOR)
        total = max(position - len(STREAM_SEPARATOR), 0)
        if total > MAX_STREAM_CHARS:
            raise ValueError("document text exceeds the maximum size")
        if self.stats.chars != total:
            raise ValueError("stats.chars must equal the length of the block text stream")
        if self.modality != "pdf" and self.stats.pages is not None:
            raise ValueError("only pdf documents report pages")
        if self.modality == "pdf":
            if self.stats.pages is None:
                raise ValueError("a pdf document reports its page count")
            for block in self.blocks:
                if (block.locator.page or 0) > self.stats.pages:
                    raise ValueError("block page beyond the page count")
        return self

    def stream(self) -> str:
        """The normalized block text stream all locator offsets refer to."""
        return STREAM_SEPARATOR.join(block.text for block in self.blocks)


class Chunk(_Model):
    """A retrievable unit of text.

    ``locators`` has one entry per block the chunk spans, in order; each covers exactly the part of
    that block's text the chunk contains (``char_start``/``char_end`` into the document stream), so

        chunk.text == "\\n\\n".join(stream[loc.char_start:loc.char_end] for loc in chunk.locators)

    ``line_start``/``line_end`` of a locator are those of the whole block (a superset).
    ``heading_path`` is the section path in force where the chunk starts.
    """

    chunk_index: Annotated[int, Field(ge=0)]
    chunk_id: uuid.UUID
    text: Annotated[str, StringConstraints(min_length=1, max_length=MAX_STREAM_CHARS)]
    token_estimate: Annotated[int, Field(ge=1)]
    content_hash: Sha256Hex
    locators: Annotated[list[Locator], Field(min_length=1, max_length=MAX_CHUNK_LOCATORS)]
    heading_path: Annotated[
        list[Annotated[str, StringConstraints(max_length=MAX_SECTION_TITLE_CHARS)]],
        Field(max_length=MAX_SECTION_DEPTH),
    ]
    page_start: Annotated[int, Field(ge=1)] | None = None
    page_end: Annotated[int, Field(ge=1)] | None = None

    @property
    def char_start(self) -> int:
        return self.locators[0].char_start

    @property
    def char_end(self) -> int:
        return self.locators[-1].char_end

    @field_validator("text")
    @classmethod
    def _text_ok(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a chunk must contain non-whitespace text")
        return _utf8_safe(value)

    @model_validator(mode="after")
    def _derived_fields(self) -> Self:
        if self.token_estimate != math.ceil(len(self.text) / 4):
            raise ValueError("token_estimate must be ceil(len(text)/4)")
        if self.content_hash != hashlib.sha256(self.text.encode("utf-8")).hexdigest():
            raise ValueError("content_hash must be the sha256 of the text")
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("page_start and page_end go together")
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError("page_end must be >= page_start")
        return self
