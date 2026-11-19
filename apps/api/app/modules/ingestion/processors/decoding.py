"""Bytes to text: BOM handling, strict UTF-8, an optional declared charset, then detection.

Deterministic for a given ``charset-normalizer`` version. Never returns replacement-character text
silently: undecodable input is ``encoding_unreadable``.
"""

from __future__ import annotations

import codecs
import re
from typing import Final

from charset_normalizer import from_bytes

from app.modules.ingestion.contracts import ProcessingError

_BOMS: Final[tuple[tuple[bytes, str], ...]] = (
    # UTF-32 LE starts with the UTF-16 LE BOM, so it must be tested first.
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)
_CHARSET_PARAM = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9_.:+-]{1,40})", re.IGNORECASE)
_WESTERN: Final = frozenset({"cp1252", "latin_1", "iso8859_1", "iso8859_15"})
# More than this share of control characters means "this is not text".
_MAX_CONTROL_SHARE = 0.30
_CONTROLS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def charset_from_content_type(content_type: str) -> str | None:
    match = _CHARSET_PARAM.search(content_type.partition(";")[2])
    return match.group(1) if match else None


def _strict(data: bytes, encoding: str) -> str | None:
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def _usable_codec(name: str) -> str | None:
    try:
        info = codecs.lookup(name)
    except LookupError:
        return None
    if not getattr(info, "_is_text_encoding", True):
        return None
    return info.name


def decode_bytes(data: bytes, *, hint: str | None = None) -> str:
    """Decode text bytes (see module docstring). ``hint`` is a declared charset name."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            text = _strict(data[len(bom) :], encoding)
            if text is None:
                raise ProcessingError("encoding_unreadable", detail=f"invalid {encoding} data")
            return _reject_binary(text)
    text = _strict(data, "utf-8")
    if text is not None:
        return _reject_binary(text)
    if hint:
        codec = _usable_codec(hint)
        if codec:
            text = _strict(data, codec)
            if text is not None:
                return _reject_binary(text)
    matches = from_bytes(data)
    best = matches.best()
    if best is None:
        raise ProcessingError("encoding_unreadable", detail="encoding could not be detected")
    # Detection of short Western-European text is unreliable (Windows-1250/1257 often outrank
    # Windows-1252 for French or German), so a Western candidate wins when the detector lists one.
    for candidate in matches:
        if candidate.encoding in _WESTERN:
            best = candidate
            break
    return _reject_binary(str(best))


def _reject_binary(text: str) -> str:
    if text and len(text) - len(_CONTROLS.sub("", text)) > len(text) * _MAX_CONTROL_SHARE:
        raise ProcessingError("encoding_unreadable", detail="data looks binary, not text")
    return text
