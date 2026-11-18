"""Pure helpers shared by every ObjectStore implementation: header values and validation."""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from urllib.parse import quote

MAX_PRESIGN_SECONDS = 7 * 24 * 3600  # SigV4 limit
DEFAULT_DOWNLOAD_NAME = "download"
_MIME_TOKEN = r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}"  # noqa: S105  # a regex, not a secret
_CONTENT_TYPE_RE = re.compile(f"{_MIME_TOKEN}/{_MIME_TOKEN}")
_ATTR_CHAR_SAFE = "!#$&+-.^_`|~"  # RFC 5987 attr-char punctuation (alphanumerics are always safe)
_ASCII_FALLBACK_RE = re.compile(r"[^A-Za-z0-9._ -]")
_BAD_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f/\\]")
_SPACES_RE = re.compile(r"\s+")
_LINE_SPACE_RE = re.compile(r"[\t\n\r\v\f]")


def sanitize_filename(filename: str) -> str:
    """Strip path separators, control characters and bidi/format characters; collapse whitespace.

    Never returns an empty string (falls back to ``download``) and is at most 255 characters.
    """
    spaced = _LINE_SPACE_RE.sub(" ", filename)  # tabs and newlines become spaces, not nothing
    cleaned = "".join(ch for ch in spaced if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    cleaned = _BAD_CHARS_RE.sub(" ", cleaned)
    cleaned = _SPACES_RE.sub(" ", cleaned).lstrip(". ")
    cleaned = cleaned[:255].strip()
    return cleaned or DEFAULT_DOWNLOAD_NAME


def content_disposition(filename: str, *, inline: bool) -> str:
    """``inline|attachment; filename="<ascii>"; filename*=UTF-8''<rfc5987>`` (sanitized)."""
    name = sanitize_filename(filename)
    ascii_name = _ASCII_FALLBACK_RE.sub("_", name)
    encoded = quote(name, safe=_ATTR_CHAR_SAFE)
    disposition = "inline" if inline else "attachment"
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def is_valid_content_type(value: str) -> bool:
    """Bare ``type/subtype`` without parameters, whitespace or control characters."""
    return bool(_CONTENT_TYPE_RE.fullmatch(value))


def validate_content_type(value: str) -> str:
    """Return ``value`` stripped if its ``type/subtype`` is valid (``;`` parameters allowed)."""
    base_type = value.split(";", 1)[0].strip()
    if not is_valid_content_type(base_type) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("invalid content type")
    return value.strip()


def validate_sha256_b64(value: str) -> str:
    """The base64 of a 32-byte digest (what ``x-amz-checksum-sha256`` carries)."""
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("sha256_b64 must be base64") from None
    if len(raw) != 32 or base64.b64encode(raw).decode() != value:
        raise ValueError("sha256_b64 must be the base64 of a 32-byte SHA-256 digest")
    return value


def validate_expiry(expires_seconds: int) -> int:
    if isinstance(expires_seconds, bool) or not 1 <= expires_seconds <= MAX_PRESIGN_SECONDS:
        raise ValueError(f"expires_seconds must be between 1 and {MAX_PRESIGN_SECONDS}")
    return expires_seconds


def validate_part_number(part_number: int) -> int:
    if isinstance(part_number, bool) or not 1 <= part_number <= 10_000:
        raise ValueError("part_number must be between 1 and 10000")
    return part_number
