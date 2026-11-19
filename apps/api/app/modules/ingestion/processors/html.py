"""HTML processor: BeautifulSoup with the stdlib parser, text only.

Nothing is executed, fetched or resolved: ``src``/``href``/``srcset``/``data`` attributes are never
read, and no network or file API is used (the tests run it with sockets disabled). Scripts, styles,
templates, frames, embedded objects, form controls, comments and hidden elements are dropped.
All tree walks are iterative with explicit stacks, so nesting depth cannot overflow the stack.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Final

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from app.modules.ingestion.contracts import ExtractedDocument, ProcessingError, SourceInfo
from app.modules.ingestion.processors.base import (
    DocumentBuilder,
    Limits,
    SectionTracker,
    check_input_size,
)
from app.modules.ingestion.processors.decoding import charset_from_content_type, decode_bytes

NAME = "html"

_REMOVED: Final = frozenset(
    {
        "head", "script", "style", "noscript", "template", "iframe", "frame", "frameset", "object",
        "embed", "applet", "svg", "math", "canvas", "audio", "video", "source", "track", "map",
        "input", "button", "select", "option", "optgroup", "datalist", "textarea", "output",
        "meter", "progress", "link", "meta", "base", "param", "dialog",
    }
)  # fmt: skip
_BLOCK: Final = frozenset(
    {
        "address", "article", "aside", "blockquote", "body", "caption", "dd", "details",
        "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
        "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "html", "li", "main", "nav", "ol",
        "p", "pre", "section", "summary", "table", "tbody", "td", "tfoot", "th", "thead",
        "tr", "ul", "legend",
    }
)  # fmt: skip
_HEADINGS: Final = {f"h{n}": n for n in range(1, 7)}
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_META_CHARSET = re.compile(rb"<meta[^>]{0,200}?charset\s*=\s*[\"']?([A-Za-z0-9_.:+-]{1,40})", re.I)
_SPACE_RUN = re.compile(r"\s+")


def _hidden(tag: Tag) -> bool:
    attrs = tag.attrs
    if "hidden" in attrs:
        return True
    if str(attrs.get("aria-hidden", "")).strip().lower() == "true":
        return True
    style = attrs.get("style")
    return isinstance(style, str) and bool(_HIDDEN_STYLE.search(style))


def _skipped(tag: Tag) -> bool:
    name = (tag.name or "").lower()
    return name in _REMOVED or _hidden(tag)


def _plain_text_nodes(tag: Tag) -> Iterator[Tag | str]:
    """Pre-order walk yielding visible element starts and text nodes (iterative; skips junk)."""
    stack: list[Iterator[object]] = [iter(tag.children)]
    while stack:
        node = next(stack[-1], None)
        if node is None:
            stack.pop()
        elif isinstance(node, Tag):
            if _skipped(node):
                continue
            yield node
            stack.append(iter(node.children))
        elif type(node) is NavigableString:  # excludes Comment, CData, Doctype, Script, ...
            yield str(node)


def _inline_text(tag: Tag, *, block_sep: str, skip: frozenset[str] = frozenset()) -> str:
    """Visible text of a subtree; block-level tags and ``<br>`` become ``block_sep``."""
    parts: list[str] = []
    stack: list[Iterator[object]] = [iter(tag.children)]
    exits: list[bool] = []  # whether leaving that open element emits a separator
    while stack:
        node = next(stack[-1], None)
        if node is None:
            stack.pop()
            if exits and exits.pop():
                parts.append(block_sep)
        elif isinstance(node, Tag):
            name = (node.name or "").lower()
            if _skipped(node) or name in skip:
                continue
            if name == "br":
                parts.append(block_sep)
                continue
            is_block = name in _BLOCK
            if is_block:
                parts.append(block_sep)
            exits.append(is_block)
            stack.append(iter(node.children))
        elif type(node) is NavigableString:
            parts.append(str(node))
    return "".join(parts)


def _raw_text(tag: Tag) -> str:
    """Visible text with layout kept (for ``<pre>``): ``<br>`` is a newline."""
    parts: list[str] = []
    stack: list[Iterator[object]] = [iter(tag.children)]
    while stack:
        node = next(stack[-1], None)
        if node is None:
            stack.pop()
        elif isinstance(node, Tag):
            if _skipped(node):
                continue
            if (node.name or "").lower() == "br":
                parts.append("\n")
                continue
            stack.append(iter(node.children))
        elif type(node) is NavigableString:
            parts.append(str(node))
    return "".join(parts)


def _descendants(tag: Tag, names: frozenset[str]) -> Iterator[Tag]:
    for node in _plain_text_nodes(tag):
        if isinstance(node, Tag) and (node.name or "").lower() in names:
            yield node


def _table_text(table: Tag) -> str:
    rows: list[str] = []
    for child in table.children:
        if (
            isinstance(child, Tag)
            and (child.name or "").lower() == "caption"
            and not _skipped(child)
        ):
            rows.append(" ".join(_inline_text(child, block_sep=" ").split()))
    for tr in _descendants(table, frozenset({"tr"})):
        cells = []
        for cell in tr.children:
            if (
                isinstance(cell, Tag)
                and (cell.name or "").lower() in ("td", "th")
                and not _skipped(cell)
            ):
                cells.append(
                    _SPACE_RUN.sub(
                        " ", _inline_text(cell, block_sep=" ", skip=frozenset({"table"}))
                    ).strip()
                )
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _list_text(root: Tag) -> str:
    items: list[str] = []
    counter = 0
    for li in _descendants(root, frozenset({"li"})):
        body = " ".join(_inline_text(li, block_sep=" ", skip=frozenset({"ul", "ol"})).split())
        if not body:
            continue
        if li.parent is root and (root.name or "").lower() == "ol":
            counter += 1
            items.append(f"{counter}. {body}")
        else:
            items.append(f"- {body}")
    return "\n".join(items)


def _declared_charset(data: bytes) -> str | None:
    match = _META_CHARSET.search(data[:4096])
    return match.group(1).decode("ascii", "replace") if match else None


def extract_html(
    data: bytes, content_type: str, *, source: SourceInfo, limits: Limits
) -> ExtractedDocument:
    check_input_size(data, limits)
    if not data.strip():
        raise ProcessingError("no_text_extracted", detail="empty input")
    elements = data.count(b"<") - data.count(b"</")
    if elements > limits.max_html_elements:
        raise ProcessingError("too_large", detail=f"more than {limits.max_html_elements} elements")
    hint = charset_from_content_type(content_type) or _declared_charset(data)
    text = decode_bytes(data, hint=hint)
    try:
        soup = BeautifulSoup(text, "html.parser")
        return _walk(soup, source, limits)
    except ProcessingError:
        raise
    except (RecursionError, MemoryError, ValueError, TypeError, AttributeError) as exc:
        raise ProcessingError(
            "malformed_input", detail=f"html parse failure: {type(exc).__name__}"
        ) from exc


def _walk(soup: BeautifulSoup, source: SourceInfo, limits: Limits) -> ExtractedDocument:
    builder = DocumentBuilder("html", source, NAME, limits)
    sections = SectionTracker()
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            builder.add("paragraph", "".join(buffer), section_path=sections.path())
            buffer.clear()

    stack: list[Iterator[object]] = [iter(soup.children)]
    exits: list[bool] = []  # per open element: does leaving it end a paragraph (block-level)?
    while stack:
        node = next(stack[-1], None)
        if node is None:
            stack.pop()
            if exits and exits.pop():
                flush()
            continue
        if type(node) is NavigableString:
            buffer.append(str(node))
            continue
        if not isinstance(node, Tag) or _skipped(node):
            continue
        name = (node.name or "").lower()
        if name in _HEADINGS:
            flush()
            title = " ".join(_inline_text(node, block_sep=" ").split())
            if title:
                path = sections.push(_HEADINGS[name], title)
                builder.add("heading", title, section_path=path, level=_HEADINGS[name])
        elif name == "pre":
            flush()
            builder.add("code", _raw_text(node), section_path=sections.path())
        elif name == "table":
            flush()
            builder.add("table", _table_text(node), section_path=sections.path())
        elif name in ("ul", "ol"):
            flush()
            builder.add("list", _list_text(node), section_path=sections.path())
        elif name == "blockquote":
            flush()
            builder.add("quote", _inline_text(node, block_sep="\n"), section_path=sections.path())
        elif name == "br":
            buffer.append("\n")
        else:
            is_block = name in _BLOCK
            if is_block:
                flush()
            exits.append(is_block)
            stack.append(iter(node.children))
    flush()
    return builder.build()
