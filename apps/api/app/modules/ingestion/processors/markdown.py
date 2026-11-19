"""Markdown processor: a small line-based block parser (no HTML is rendered, nothing is fetched).

Handles ATX and Setext headings (sections with ``section_path``), fenced code (kept verbatim as
``code`` blocks), bullet/ordered lists, block quotes, GFM tables, YAML front matter (ignored), HTML
comments and link-reference definitions (dropped). Inline markup is reduced to its text (links and
images keep their label/alt text, raw HTML tags are removed); URLs are discarded, never resolved.
Every regular expression is bounded or avoided so hostile input stays linear.
"""

from __future__ import annotations

import html
import re

from app.modules.ingestion.chunking import clean_newlines_and_controls
from app.modules.ingestion.contracts import (
    BlockKind,
    ExtractedDocument,
    ProcessingError,
    SourceInfo,
)
from app.modules.ingestion.processors.base import (
    DocumentBuilder,
    Limits,
    SectionTracker,
    check_input_size,
)
from app.modules.ingestion.processors.decoding import charset_from_content_type, decode_bytes

NAME = "markdown"

_LINK = re.compile(r"!?\[([^\]\n]{0,1000})\]\([^)\n]{0,2000}\)")
_REF_LINK = re.compile(r"\[([^\]\n]{0,1000})\]\[[^\]\n]{0,200}\]")
_AUTOLINK = re.compile(r"<(https?://[^>\s]{1,2000})>")
_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]{0,30}(?:\s[^>\n]{0,1000})?/?>")
_COMMENT = re.compile(r"<!--[^\n]{0,2000}?-->")
_REF_DEF = re.compile(r"^ {0,3}\[[^\]\n]{1,200}\]:[ \t]*\S")
_ORDERED = re.compile(r"\d{1,9}[.)][ \t]")
_TABLE_CELL = re.compile(r":?-+:?")


def _inline(text: str) -> str:
    """Reduce inline markup to text. URLs are dropped, never followed."""
    if "<" in text:
        text = _COMMENT.sub("", text)
        text = _AUTOLINK.sub(r"\1", text)
        text = _TAG.sub("", text)
    if "[" in text:
        text = _LINK.sub(r"\1", text)
        text = _REF_LINK.sub(r"\1", text)
    if "&" in text:
        text = html.unescape(text)
    return text


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _fence(line: str) -> tuple[str, int] | None:
    """(fence char, length) when the line opens or closes a fenced code block."""
    if _indent(line) > 3:
        return None
    s = line.lstrip(" ")
    if not s or s[0] not in "`~":
        return None
    char = s[0]
    n = len(s) - len(s.lstrip(char))
    if n < 3:
        return None
    if char == "`" and "`" in s[n:]:
        return None
    return char, n


def _atx(line: str) -> tuple[int, str] | None:
    if _indent(line) > 3:
        return None
    s = line.lstrip(" ")
    if not s.startswith("#"):
        return None
    n = len(s) - len(s.lstrip("#"))
    if n > 6 or (len(s) > n and s[n] not in " \t"):
        return None
    title = s[n:].strip()
    stripped = title.rstrip("#")
    if stripped != title and (not stripped or stripped[-1] in " \t"):
        title = stripped.rstrip()
    return n, title


def _is_rule(line: str) -> bool:
    s = line.strip()
    return len(s) >= 3 and s[0] in "-*_" and set(s) <= {s[0], " ", "\t"} and s.count(s[0]) >= 3


def _setext_level(line: str) -> int | None:
    if _indent(line) > 3:
        return None
    s = line.strip()
    if s and set(s) == {"="}:
        return 1
    if s and set(s) == {"-"}:
        return 2
    return None


def _is_list_item(line: str) -> bool:
    if _indent(line) > 12:
        return False
    s = line.lstrip(" ")
    if len(s) > 2 and s[0] in "-*+" and s[1] in " \t" and s[2:].strip():
        return True
    m = _ORDERED.match(s)
    return bool(m and s[m.end() :].strip())


def _same_list(first: str, other: str) -> bool:
    """Both are list items of the same marker family (bullet or ordered)."""
    if not _is_list_item(other):
        return False
    if _indent(other) >= 2:
        return True
    first_ordered = _ORDERED.match(first.lstrip(" ")) is not None
    return first_ordered == (_ORDERED.match(other.lstrip(" ")) is not None)


def _is_quote(line: str) -> bool:
    return _indent(line) <= 3 and line.lstrip(" ").startswith(">")


def _is_table_separator(line: str) -> bool:
    s = line.strip()
    if "|" not in s or not s.strip("|").strip():
        return False
    cells = s.strip("|").split("|")
    return all(_TABLE_CELL.fullmatch(c.strip()) for c in cells)


def _starts_block(line: str) -> bool:
    return bool(
        _fence(line)
        or _atx(line) is not None
        or _is_rule(line)
        or _is_quote(line)
        or _is_list_item(line)
    )


def _unquote(line: str) -> str:
    s = line.lstrip(" ")
    while s.startswith(">"):
        s = s[1:]
        if s.startswith(" "):
            s = s[1:]
    return s


def extract_markdown(
    data: bytes, content_type: str, *, source: SourceInfo, limits: Limits
) -> ExtractedDocument:
    check_input_size(data, limits)
    if not data.strip():
        raise ProcessingError("no_text_extracted", detail="empty input")
    text = decode_bytes(data, hint=charset_from_content_type(content_type))
    lines = clean_newlines_and_controls(text).split("\n")
    n = len(lines)
    builder = DocumentBuilder("markdown", source, NAME, limits)
    sections = SectionTracker()

    i = 0
    if lines and lines[0].strip() == "---":
        for j in range(1, n):
            if lines[j].strip() in ("---", "..."):
                i = j + 1  # YAML front matter is metadata, not content
                break

    para: list[str] = []
    para_start = 0

    def add(kind: BlockKind, body: str, first: int, last: int, level: int | None = None) -> None:
        builder.add(
            kind,
            body,
            line_start=first + 1,
            line_end=last + 1,
            section_path=sections.path(),
            level=level,
        )

    def flush_para() -> None:
        if para:
            add("paragraph", _inline("\n".join(para)), para_start, para_start + len(para) - 1)
            para.clear()

    while i < n:
        line = lines[i]
        if not line.strip():
            flush_para()
            i += 1
            continue
        if line.lstrip().startswith("<!--") and "-->" not in line:
            flush_para()
            while i < n and "-->" not in lines[i]:
                i += 1
            i += 1
            continue
        fence = _fence(line)
        if fence:
            flush_para()
            char, size = fence
            first = i
            i += 1
            body: list[str] = []
            while i < n:
                closing = _fence(lines[i])
                if (
                    closing
                    and closing[0] == char
                    and closing[1] >= size
                    and not (lines[i].strip()[closing[1] :].strip())
                ):
                    break
                body.append(lines[i])
                i += 1
            add("code", "\n".join(body), first, min(i, n - 1))
            i += 1
            continue
        atx = _atx(line)
        if atx:
            flush_para()
            level, title = atx
            title = _inline(title)
            if title.strip():
                sections.push(level, title)
                add("heading", title, i, i, level=level)
            i += 1
            continue
        level2 = _setext_level(line)
        if para and level2 is not None:
            title = _inline(" ".join(p.strip() for p in para))
            first = para_start
            para.clear()
            sections.push(level2, title)
            add("heading", title, first, i, level=level2)
            i += 1
            continue
        if _is_rule(line):
            flush_para()
            i += 1
            continue
        if _REF_DEF.match(line) and not para:
            i += 1
            continue
        if _is_quote(line):
            flush_para()
            first = i
            body = []
            while (
                i < n and lines[i].strip() and (_is_quote(lines[i]) or not _starts_block(lines[i]))
            ):
                body.append(_unquote(lines[i]))
                i += 1
            add("quote", _inline("\n".join(body)), first, i - 1)
            continue
        if _is_list_item(line):
            flush_para()
            first = i
            body = []
            while i < n:
                cur = lines[i]
                if not cur.strip():
                    k = i + 1
                    while k < n and not lines[k].strip():
                        k += 1
                    if k < n and (_same_list(line, lines[k]) or _indent(lines[k]) >= 2):
                        i = k
                        continue
                    break
                if _is_list_item(cur) and _indent(cur) < 2 and not _same_list(line, cur):
                    break  # a bullet list followed by an ordered one (or vice versa) is two lists
                if _is_list_item(cur) or _indent(cur) >= 1 or not _starts_block(cur):
                    body.append(cur.strip())
                    i += 1
                    continue
                break
            add("list", _inline("\n".join(body)), first, i - 1)
            continue
        if "|" in line and i + 1 < n and _is_table_separator(lines[i + 1]):
            flush_para()
            first = i
            rows = [line]
            i += 2
            while i < n and lines[i].strip() and not _starts_block(lines[i]):
                rows.append(lines[i])
                i += 1
            cleaned = [
                " | ".join(c.strip() for c in _inline(r.strip().strip("|")).split("|"))
                for r in rows
            ]
            add("table", "\n".join(cleaned), first, i - 1)
            continue
        if not para:
            para_start = i
        para.append(line)
        i += 1
    flush_para()
    return builder.build()
