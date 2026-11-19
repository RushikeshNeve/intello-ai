"""Text normalization and deterministic chunking (M03 contract section 7, M03.02.03).

Pure functions, no I/O. The same ``ExtractedDocument`` (and revision id) always yields
byte-identical chunks.

The offset invariant (tested, also as a property test)
-----------------------------------------------------
Let ``stream = doc.stream()`` (block texts joined by two newlines). For every chunk ``c``::

    c.text == "\\n\\n".join(stream[l.char_start:l.char_end] for l in c.locators)

with exactly one locator per spanned block, in order. A chunk cut out of the middle of a long block
has one locator whose slice *is* the chunk text. Overlap exists only between consecutive chunks of
the same long block (their slices overlap); whole blocks are never repeated, so there is no
"overlap join" and the invariant is exact.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Final

from app.modules.ingestion.contracts import (
    STREAM_SEPARATOR,
    Block,
    Chunk,
    ExtractedDocument,
    Locator,
    ProcessingError,
)
from app.modules.ingestion.ids import chunk_id_for

DEFAULT_TARGET_CHARS: Final = 1800
DEFAULT_OVERLAP_CHARS: Final = 200
DEFAULT_MAX_CHARS: Final = 2400
DEFAULT_MAX_CHUNKS: Final = 20_000


def _chars(*codes: int) -> str:
    return "".join(chr(code) for code in codes)


# Code points are spelled as numbers so no ambiguous or invisible character sits in this source.
# Newline variants: VT, FF, NEL, LINE SEPARATOR, PARAGRAPH SEPARATOR (CRLF and CR handled apart).
_NEWLINE_VARIANTS = re.compile(
    "\r\n|[" + re.escape(_chars(0x0D, 0x0B, 0x0C, 0x85, 0x2028, 0x2029)) + "]"
)
# C0 controls except TAB and LF, DEL, C1 controls, BOM / zero-width no-break space, the two
# non-characters U+FFFE/U+FFFF and lone surrogates (which cannot be encoded as UTF-8).
_STRIP = re.compile(
    "[\x00-\x08\x0e-\x1f\x7f-\x9f"
    + re.escape(_chars(0xFEFF, 0xFFFE, 0xFFFF))
    + "-".join([_chars(0xD800), _chars(0xDFFF)])
    + "]"
)
# Horizontal whitespace: space, tab, NBSP, Ogham space, U+2000..U+200A, narrow NBSP, medium
# mathematical space, ideographic space.
_HSPACE = re.compile(
    "["
    + " \t"
    + re.escape(_chars(0xA0, 0x1680))
    + re.escape(_chars(0x2000))
    + "-"
    + re.escape(_chars(0x200A))
    + re.escape(_chars(0x202F, 0x205F, 0x3000))
    + "]+"
)
_SPACE_AROUND_NL = re.compile(r" *\n *")
_BLANK_RUNS = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r" +\n")


def clean_newlines_and_controls(text: str) -> str:
    """Unify every newline variant to LF and strip control characters (keeps tabs)."""
    text = _NEWLINE_VARIANTS.sub("\n", text)
    return _STRIP.sub("", text)


def normalize_text(text: str, *, preserve_layout: bool = False) -> str:
    """Normalize text to the canonical form block texts are stored in (idempotent).

    Default: newlines unified to LF, control characters stripped (tabs are whitespace), every run of
    horizontal whitespace collapsed to one space, spaces around newlines removed, three or more
    newlines reduced to a paragraph break (two LFs), leading/trailing whitespace stripped, NFC.

    ``preserve_layout`` (code blocks): same newline/control handling and NFC, but indentation and
    internal blank lines survive (a tab becomes four spaces; trailing spaces of lines and leading
    blank lines are dropped).
    """
    text = clean_newlines_and_controls(text)
    if preserve_layout:
        text = text.replace("\t", "    ")
        text = _TRAILING_SPACE.sub("\n", text).rstrip(" \n").lstrip("\n")
    else:
        text = _HSPACE.sub(" ", text)
        text = _SPACE_AROUND_NL.sub("\n", text)
        text = _BLANK_RUNS.sub("\n\n", text).strip(" \n")
    if not unicodedata.is_normalized("NFC", text):
        text = unicodedata.normalize("NFC", text)
    return text


def normalize_block_text(text: str, kind: str) -> str:
    return normalize_text(text, preserve_layout=(kind == "code"))


@dataclass(frozen=True, slots=True)
class _Part:
    """A slice ``[start, end)`` of one block's text."""

    block: Block
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


# Sentence ends: . ! ? ellipsis, ideographic full stop, fullwidth ! and ?, danda, Arabic ?.
_SENTENCE_END: Final = frozenset(".!?" + _chars(0x2026, 0x3002, 0xFF01, 0xFF1F, 0x0964, 0x061F))
# closing quotes/brackets that may follow a sentence end: " ' ) ] } right quotes, guillemet.
_CLOSERS: Final = frozenset("\"')]}" + _chars(0x201D, 0x2019, 0xBB))


def _is_boundary(text: str, i: int) -> bool:
    """True when a chunk may end just before ``text[i]`` (after a sentence end or a newline)."""
    if i <= 0 or i >= len(text):
        return False
    if text[i - 1] == "\n":
        return True
    if not text[i].isspace():
        return False
    j = i - 1
    while j > 0 and text[j] in _CLOSERS:
        j -= 1
    return text[j] in _SENTENCE_END


def _split_long(text: str, target: int, overlap: int) -> list[tuple[int, int]]:
    """Cut one long text into windows ``(start, end)`` of at most ``target`` characters.

    Windows end at a sentence boundary when one exists in the second half of the window, otherwise
    at whitespace, otherwise hard at ``target``. Each next window starts ``overlap`` characters
    before the previous end (moved forward to a word start). Whitespace at both edges is trimmed, so
    no window is empty. Linear in ``len(text)`` for fixed target/overlap.
    """
    n = len(text)
    windows: list[tuple[int, int]] = []
    start = 0
    min_len = target // 2 + 1
    while start < n:
        if n - start <= target:
            cut = n
        else:
            hard = start + target
            floor = start + min_len
            cut = -1
            for i in range(hard, floor - 1, -1):
                if _is_boundary(text, i):
                    cut = i
                    break
            if cut < 0:
                for i in range(hard, floor - 1, -1):
                    if text[i].isspace():
                        cut = i
                        break
            if cut < 0:
                cut = hard
        lo, hi = start, cut
        while lo < hi and text[lo].isspace():
            lo += 1
        while hi > lo and text[hi - 1].isspace():
            hi -= 1
        if hi > lo:
            windows.append((lo, hi))
        if cut >= n:
            break
        nxt = cut - overlap
        if overlap > 0:
            # start the next window at a word start when one exists inside the overlap
            probe = nxt
            while probe < cut and not text[probe].isspace():
                probe += 1
            if probe < cut:
                nxt = probe + 1
        start = max(nxt, start + 1)
    return windows


def chunk_document(
    doc: ExtractedDocument,
    revision_id: uuid.UUID,
    *,
    target_chars: int = DEFAULT_TARGET_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> list[Chunk]:
    """Cut a document into chunks on block, then sentence, boundaries (see module docstring).

    Small blocks are packed whole into a chunk up to ``target_chars``; a block longer than
    ``max_chars`` is cut into overlapping windows of at most ``target_chars``; a block between
    ``target_chars`` and ``max_chars`` stays whole in its own chunk. A heading is never left
    stranded at the end of a chunk. Chunk text never exceeds ``max_chars``.
    """
    if not (0 < target_chars <= max_chars):
        raise ValueError("require 0 < target_chars <= max_chars")
    if not (0 <= overlap_chars <= target_chars // 2):
        raise ValueError("require 0 <= overlap_chars <= target_chars // 2")
    if max_chunks < 1:
        raise ValueError("max_chunks must be >= 1")

    chunks: list[Chunk] = []
    current: list[_Part] = []
    current_len = 0
    sep = len(STREAM_SEPARATOR)

    def flush(parts: list[_Part]) -> None:
        if not parts:
            return
        if len(chunks) >= max_chunks:
            raise ProcessingError("too_large", detail=f"more than {max_chunks} chunks")
        chunks.append(_make_chunk(len(chunks), parts, revision_id))

    for block in doc.blocks:
        if normalize_block_text(block.text, block.kind) != block.text:
            raise ProcessingError("malformed_input", detail="block text is not normalized")
        length = len(block.text)
        if length > max_chars:
            flush(current)
            windows = _split_long(block.text, target_chars, overlap_chars)
            for lo, hi in windows[:-1]:
                flush([_Part(block, lo, hi)])
            lo, hi = windows[-1]
            current = [_Part(block, lo, hi)]
            current_len = hi - lo
            continue
        if current and current_len + sep + length > target_chars:
            carry: list[_Part] = []
            # never leave a heading stranded at the end of a chunk
            if len(current) > 1 and current[-1].block.kind == "heading":
                carry = [current.pop()]
            flush(current)
            current = carry
            current_len = sum(p.length for p in current)
            if current and current_len + sep + length > max_chars:
                flush(current)
                current = []
        current_len = current_len + sep + length if current else length
        current.append(_Part(block, 0, length))
    flush(current)

    if not chunks:
        raise ProcessingError("no_text_extracted", detail="document yielded no chunks")
    return chunks


def _make_chunk(index: int, parts: list[_Part], revision_id: uuid.UUID) -> Chunk:
    text = STREAM_SEPARATOR.join(p.block.text[p.start : p.end] for p in parts)
    locators: list[Locator] = []
    pages: list[int] = []
    for p in parts:
        loc = p.block.locator
        base = loc.char_start
        locators.append(
            Locator(
                kind=loc.kind,
                page=loc.page,
                char_start=base + p.start,
                char_end=base + p.end,
                line_start=loc.line_start,
                line_end=loc.line_end,
                section_path=loc.section_path,
            )
        )
        if loc.page is not None:
            pages.append(loc.page)
    return Chunk(
        chunk_index=index,
        chunk_id=chunk_id_for(revision_id, index),
        text=text,
        token_estimate=math.ceil(len(text) / 4),
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        locators=locators,
        heading_path=list(parts[0].block.locator.section_path or []),
        page_start=min(pages) if pages else None,
        page_end=max(pages) if pages else None,
    )
