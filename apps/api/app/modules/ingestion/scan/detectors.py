"""Pure detectors of the ingestion scan (M14.02): bytes (+ extracted text) in, findings out.

Findings carry a rule id, a severity, a page/offset/entry *location* and at most a 40-char
*redacted* preview (the first characters, everything else masked); never document text.

Rules (``high`` is what ``quarantine`` mode acts on):

* ``hidden_instruction`` (high/medium): text addressed to an AI ("ignore previous instructions",
  "system prompt", assistant-directed imperatives).
* ``invisible_unicode`` (high/medium): bidi controls and Unicode tag characters (high), runs of
  zero-width characters (medium).
* ``hidden_text`` (medium): HTML text that is display:none, tiny, transparent or white-on-white.
* ``suspicious_link`` (high/medium): javascript:/data:/vbscript: targets, anchor text naming a
  different host than the link target.
* ``office_macro`` / ``embedded_executable`` / ``embedded_object`` / ``pdf_active_content`` (high or
  medium): macros, executables, OLE objects, JavaScript or launch actions in PDF.

Not available from the extractors (documented gaps): PDF white/tiny text and a text-to-visible
mismatch (the extractors expose only the text, no rendering).
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from app.modules.ingestion.contracts import ExtractedDocument

SEVERITIES: Final = ("low", "medium", "high")
MAX_FINDINGS: Final = 200
MAX_PER_RULE: Final = 20
PREVIEW_CHARS: Final = 40


@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    severity: str
    location: str
    preview: str = ""


def redact(snippet: str) -> str:
    """At most 40 chars: the first 3 characters stay, every other letter/digit becomes ``*``."""
    flat = " ".join(snippet.split())[:PREVIEW_CHARS]
    return "".join(ch if i < 3 or not ch.isalnum() else "*" for i, ch in enumerate(flat))


def max_severity(findings: list[Finding]) -> str | None:
    best = -1
    for f in findings:
        best = max(best, SEVERITIES.index(f.severity))
    return None if best < 0 else SEVERITIES[best]


# ------------------------------------------------------------------------------ text rules

_INJECTION: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(
            r"\b(ignore|disregard|forget|override)\b[^.\n]{0,30}"
            r"\b(previous|prior|above|earlier|all|any|your)\b[^.\n]{0,20}"
            r"\b(instructions?|prompts?|rules|directions|guidelines)\b",
            re.IGNORECASE,
        ),
        "high",
    ),
    (
        re.compile(
            r"\b(do not|don't|never)\s+(tell|inform|reveal\s+to|mention\s+to)\s+the\s+user\b",
            re.IGNORECASE,
        ),
        "high",
    ),
    (re.compile(r"<\|?\s*(im_start|im_end|system|assistant)\s*\|?>", re.IGNORECASE), "high"),
    (re.compile(r"\byou\s+are\s+now\s+(a|an|in)\b[^.\n]{0,40}", re.IGNORECASE), "medium"),
    (re.compile(r"\b(new|updated)\s+(system\s+)?instructions\s*:", re.IGNORECASE), "medium"),
    (re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE), "medium"),
    (
        re.compile(
            r"\b(assistant|chatbot|llm|ai\s+(agent|model|assistant)|language\s+model)\b"
            r"[^.\n]{0,25}\b(must|should|shall|will|has\s+to)\s+(now\s+)?"
            r"(reveal|output|print|send|exfiltrate|ignore|execute|call|forward|respond\s+with)\b",
            re.IGNORECASE,
        ),
        "medium",
    ),
)
_BIDI = re.compile("[‪-‮⁦-⁩‎‏؜]+")
_TAGS = re.compile("[\U000e0000-\U000e007f]+")
_ZERO_WIDTH = re.compile("[​‌‍⁠﻿᠎]{3,}")


def _capped(findings: list[Finding]) -> list[Finding]:
    counts: dict[str, int] = {}
    out: list[Finding] = []
    for f in findings:
        counts[f.rule_id] = counts.get(f.rule_id, 0) + 1
        if counts[f.rule_id] <= MAX_PER_RULE:
            out.append(f)
    return out[:MAX_FINDINGS]


def scan_text(text: str, where: Callable[[int], str]) -> list[Finding]:
    """Text rules over ``text``; ``where(offset) -> str`` names the location of an offset."""
    found: list[Finding] = []
    for pattern, severity in _INJECTION:
        for m in pattern.finditer(text):
            found.append(
                Finding("hidden_instruction", severity, where(m.start()), redact(m.group(0)))
            )
    for m in _BIDI.finditer(text):
        found.append(
            Finding("invisible_unicode", "high", where(m.start()), f"{len(m.group(0))} bidi")
        )
    for m in _TAGS.finditer(text):
        found.append(
            Finding("invisible_unicode", "high", where(m.start()), f"{len(m.group(0))} tag")
        )
    for m in _ZERO_WIDTH.finditer(text):
        found.append(
            Finding(
                "invisible_unicode", "medium", where(m.start()), f"{len(m.group(0))} zero-width"
            )
        )
    return found


# ------------------------------------------------------------------------------------ links

_BAD_SCHEMES: Final = ("javascript:", "data:", "vbscript:")
_MD_LINK = re.compile(r"\[([^\]\n]{1,200})\]\(\s*<?([^)\s>]{1,2000})")
_URLISH = re.compile(r"^(?:https?://)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)", re.IGNORECASE)


def _host(url: str) -> str:
    return (
        (urlparse(url if "//" in url else "//" + url).hostname or "").lower().removeprefix("www.")
    )


def check_link(anchor: str, target: str, location: str) -> Finding | None:
    t = target.strip()
    low = re.sub(r"[\s\x00-\x1f]", "", t).lower()
    if low.startswith(_BAD_SCHEMES):
        return Finding("suspicious_link", "high", location, redact(low.split(":", 1)[0] + ":"))
    m = _URLISH.match(anchor.strip())
    if m and low.startswith(("http://", "https://")):
        shown, real = m.group(1).lower().removeprefix("www."), _host(low)
        if real and shown != real:
            return Finding("suspicious_link", "medium", location, redact(f"{shown} -> {real}"))
    return None


def _html_findings(raw: str) -> list[Finding]:
    out: list[Finding] = []
    soup = BeautifulSoup(raw, "html.parser")
    hidden_style = re.compile(
        r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(\.0+)?\s*(;|$)"
        r"|font-size\s*:\s*0*[0-2](\.\d+)?(px|pt|em|rem)?\s*(;|$)",
        re.IGNORECASE,
    )
    white = re.compile(
        r"(^|;)\s*color\s*:\s*(#f{3}|#f{6}|white|rgb\(\s*255\s*,\s*255\s*,\s*255\s*\))", re.I
    )
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        style = str(tag.get("style") or "")
        hidden = (
            tag.has_attr("hidden") or bool(hidden_style.search(style)) or bool(white.search(style))
        )
        if hidden and tag.get_text(strip=True):
            line = getattr(tag, "sourceline", None) or 0
            out.append(
                Finding(
                    "hidden_text", "medium", f"line:{line}", redact(tag.get_text(" ", strip=True))
                )
            )
        if tag.name == "a" and tag.has_attr("href"):
            f = check_link(
                tag.get_text(" ", strip=True),
                str(tag["href"]),
                f"line:{getattr(tag, 'sourceline', 0) or 0}",
            )
            if f:
                out.append(f)
    return out


# ------------------------------------------------------------------------ binary containers

_EXEC_NAME = re.compile(r"\.(exe|dll|scr|com|bat|cmd|ps1|vbs|vbe|js|jse|wsf|msi|jar|lnk|sh)$", re.I)
_PDF_ACTIVE: Final = (
    (re.compile(rb"/(JavaScript|JS)\b"), "high", "javascript"),
    (re.compile(rb"/Launch\b"), "high", "launch"),
    (re.compile(rb"/EmbeddedFile\b"), "medium", "embedded_file"),
)


def _zip_findings(data: bytes) -> list[Finding]:
    out: list[Finding] = []
    try:
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
        return out
    for name in names[:5000]:
        low = name.lower()
        loc = f"entry:{name[:150]}"
        if low.endswith("vbaproject.bin") or "/macros/" in low or low.endswith(".xlm"):
            out.append(Finding("office_macro", "high", loc))
        elif _EXEC_NAME.search(low):
            out.append(Finding("embedded_executable", "high", loc))
        elif "/embeddings/" in low or "oleobject" in low or low.endswith("activex.bin"):
            out.append(Finding("embedded_object", "medium", loc))
    return out


def _binary_findings(data: bytes) -> list[Finding]:
    out: list[Finding] = []
    head = data[:8]
    if data[:2] == b"MZ" or data[:4] == b"\x7fELF":
        out.append(Finding("embedded_executable", "high", "file"))
    if data[:4] == b"PK\x03\x04":
        out.extend(_zip_findings(data))
    if head.startswith(b"\xd0\xcf\x11\xe0") and (
        b"_VBA_PROJECT" in data or b"VBA" in data or "Macros".encode("utf-16-le") in data
    ):
        out.append(Finding("office_macro", "high", "file"))
    if data[:1024].lstrip().startswith(b"%PDF"):
        for pattern, severity, _ in _PDF_ACTIVE:
            m = pattern.search(data)
            if m:
                out.append(Finding("pdf_active_content", severity, f"offset:{m.start()}"))
        for m in re.finditer(rb"/URI\s*\(\s*([^)]{0,2000})\)", data):
            f = check_link("", m.group(1).decode("latin-1"), f"offset:{m.start()}")
            if f:
                out.append(f)
    return out


# ---------------------------------------------------------------------------------- the scan

_TEXTLIKE_PREFIX = ("text/", "application/json", "application/xml", "application/xhtml")


def _is_textlike(content_type: str, data: bytes) -> bool:
    ct = content_type.split(";")[0].strip().lower()
    if data[:4] in (b"PK\x03\x04", b"%PDF") or data[:4] == b"\xd0\xcf\x11\xe0":
        return False
    return ct.startswith(_TEXTLIKE_PREFIX) or b"\x00" not in data[:2048]


def scan_document(data: bytes, content_type: str, doc: ExtractedDocument | None) -> list[Finding]:
    """All findings of one document version (deterministic order, capped)."""
    findings: list[Finding] = []
    ct = content_type.split(";")[0].strip().lower()
    if _is_textlike(ct, data):
        raw = data.decode("utf-8", errors="replace")
        findings += scan_text(raw, lambda off: f"offset:{off}")
        if "html" in ct or raw.lstrip()[:200].lower().startswith(("<!doctype html", "<html")):
            findings += _html_findings(raw)
        else:
            for m in _MD_LINK.finditer(raw):
                f = check_link(m.group(1), m.group(2), f"offset:{m.start()}")
                if f:
                    findings.append(f)
    else:
        findings += _binary_findings(data)
        if doc is not None:
            findings += _scan_blocks(doc)
    return _capped(
        sorted(set(findings), key=lambda f: (f.rule_id, f.location, f.severity, f.preview))
    )


def _locator_of(page: int | None, base: int) -> Callable[[int], str]:
    def where(off: int) -> str:
        return f"page:{page}" if page else f"offset:{base + off}"

    return where


def _scan_blocks(doc: ExtractedDocument) -> list[Finding]:
    out: list[Finding] = []
    for block in doc.blocks:
        loc = block.locator
        out += scan_text(block.text, _locator_of(loc.page, loc.char_start))
    return out
