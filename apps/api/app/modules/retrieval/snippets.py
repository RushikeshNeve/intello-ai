"""Server-side snippets and highlight ranges (no HTML). Offsets are Unicode code points."""

from __future__ import annotations

import re
from typing import Final

_TOKEN: Final = re.compile(r"[^\W_]+")
_STOP: Final = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    ]
)
MAX_SNIPPET_CHARS: Final = 320


def query_terms(query: str) -> list[str]:
    seen: dict[str, None] = {}
    for tok in _TOKEN.findall(query.lower()):
        if tok in _STOP or (len(tok) < 2 and not tok.isdigit()):
            continue
        seen[tok] = None
    return list(seen)


def highlight_ranges(text: str, terms: list[str]) -> list[list[int]]:
    """Merged ``[start, end)`` ranges of whole-word, case-insensitive term matches."""
    if not terms:
        return []
    ordered = sorted(terms, key=lambda t: (-len(t), t))
    pattern = re.compile(
        r"(?<![^\W_])(?:" + "|".join(re.escape(t) for t in ordered) + r")(?![^\W_])",
        re.IGNORECASE,
    )
    ranges: list[list[int]] = []
    for m in pattern.finditer(text):
        s, e = m.span()
        if ranges and s <= ranges[-1][1]:
            ranges[-1][1] = max(ranges[-1][1], e)
        else:
            ranges.append([s, e])
    return ranges


def build_snippet(text: str, terms: list[str], max_chars: int = MAX_SNIPPET_CHARS) -> str:
    body = text.strip()
    if len(body) <= max_chars:
        return body
    first = highlight_ranges(body, terms)
    anchor = first[0][0] if first else 0
    start = max(0, anchor - max_chars // 4)
    end = min(len(body), start + max_chars)
    if start > 0:
        ws = body.find(" ", start, start + 24)
        start = ws + 1 if ws != -1 else start
    if end < len(body):
        ws = body.rfind(" ", max(start, end - 24), end)
        end = ws if ws != -1 else end
    snippet = body[start:end].strip()
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(body) else "")
