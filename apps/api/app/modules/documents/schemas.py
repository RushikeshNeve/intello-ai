"""Request/response models, validators and the filename sanitizer (contract m02-contracts.md §5).

Requests use ``extra="forbid"``. Validation failures surface as 422 ``invalid_request`` through
the shared ``validation_error_handler`` (``app.api.deps``).

Interpretations of the contract (reported in the M02.02.01 handoff):

* :func:`sanitize_filename` keeps only the last path component (the browser may send
  ``C:\\fakepath\\a.pdf``), removes control characters and bidirectional-override characters,
  turns every Unicode whitespace run into one space and trims. A name that is empty, or only dots,
  afterwards is invalid. The filename never reaches an object key (contract §2).
* ``content_type`` is a bare ``type/subtype`` (RFC 6838 names, no parameters), lower-cased,
  so the inline allow-list and the signed ``Content-Type`` header compare exactly.
"""

from __future__ import annotations

import base64
import re
import unicodedata
import uuid
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.models import (
    CONTENT_TYPE_MAX_LENGTH,
    FILENAME_MAX_LENGTH,
    SHA256_PATTERN,
    TITLE_MAX_LENGTH,
)
from app.modules.ingestion.schemas import IngestionSummary

_PATH_SEPARATORS: Final = re.compile(r"[\\/]")
_WHITESPACE: Final = re.compile(r"\s+")
# Bidirectional embedding/override/isolate controls (Cf) enable filename spoofing ("exe.fdp").
_BIDI_CONTROLS: Final = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_CONTENT_TYPE: Final = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)
_SHA256: Final = re.compile(SHA256_PATTERN)

SORT_FIELDS: Final = ("updated_at", "created_at", "title")
SORT_ORDERS: Final = ("asc", "desc")
DISPOSITIONS: Final = ("inline", "attachment")
DocumentSort = Literal["updated_at", "created_at", "title"]
SortOrder = Literal["asc", "desc"]
Disposition = Literal["inline", "attachment"]


def sanitize_filename(raw: str) -> str:
    """Return a safe display filename, or ``""`` when nothing usable is left.

    Does not truncate: callers reject names longer than ``FILENAME_MAX_LENGTH`` (422).
    """
    name = _PATH_SEPARATORS.split(raw)[-1]
    kept: list[str] = []
    for char in name:
        if char in _BIDI_CONTROLS:
            continue
        category = unicodedata.category(char)
        if char in "\t\n\r\x0b\x0c" or category in ("Zl", "Zp", "Zs"):
            kept.append(" ")  # tab/newline are Cc but count as whitespace here
        elif category == "Cc":
            continue
        else:
            kept.append(char)
    cleaned = _WHITESPACE.sub(" ", "".join(kept)).strip()
    if not cleaned.strip("."):
        return ""  # "", ".", ".."
    return cleaned


def normalize_content_type(value: str) -> str:
    """Lower-case ``type/subtype``; raises ``ValueError`` when it does not look like one."""
    normalized = value.strip().lower()
    if len(normalized) > CONTENT_TYPE_MAX_LENGTH or not _CONTENT_TYPE.fullmatch(normalized):
        raise ValueError("content_type must look like type/subtype")
    return normalized


def validate_sha256(value: str) -> str:
    """64 lower-case hex characters (as stored)."""
    if not _SHA256.fullmatch(value):
        raise ValueError("sha256 must be 64 lowercase hex characters")
    return value


def sha256_hex_to_b64(sha256_hex: str) -> str:
    """Base64 of the raw digest: the value of ``x-amz-checksum-sha256``."""
    return base64.b64encode(bytes.fromhex(validate_sha256(sha256_hex))).decode("ascii")


def default_title(filename: str) -> str:
    """The filename without its extension (the whole name for dotfiles or extension-only names)."""
    stem, dot, _ext = filename.rpartition(".")
    title = stem.strip() if dot and stem.strip(". ") else filename.strip()
    return title[:TITLE_MAX_LENGTH].strip() or filename.strip()[:TITLE_MAX_LENGTH]


def _title(value: str) -> str:
    title = value.strip()
    if not 1 <= len(title) <= TITLE_MAX_LENGTH:
        raise ValueError(f"title must be 1..{TITLE_MAX_LENGTH} characters")
    if any(unicodedata.category(c) == "Cc" for c in title):
        raise ValueError("title must not contain control characters")
    return title


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- requests -----------------------------------------------------------------------------


class UploadCreate(_In):
    filename: str
    content_type: str
    size_bytes: int = Field(ge=1)
    sha256: str
    document_id: uuid.UUID | None = None
    title: str | None = None

    @field_validator("filename")
    @classmethod
    def _filename(cls, value: str) -> str:
        cleaned = sanitize_filename(value)
        if not 1 <= len(cleaned) <= FILENAME_MAX_LENGTH:
            raise ValueError(f"filename must be 1..{FILENAME_MAX_LENGTH} characters")
        return cleaned

    @field_validator("content_type")
    @classmethod
    def _content_type(cls, value: str) -> str:
        return normalize_content_type(value)

    @field_validator("sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        return validate_sha256(value)

    @field_validator("title")
    @classmethod
    def _title_value(cls, value: str | None) -> str | None:
        return None if value is None else _title(value)


class DocumentUpdate(_In):
    """``PATCH`` body."""

    title: str

    @field_validator("title")
    @classmethod
    def _title_value(cls, value: str) -> str:
        return _title(value)


# --- responses ----------------------------------------------------------------------------


class PresignedUpload(BaseModel):
    method: Literal["PUT"] = "PUT"
    url: str
    headers: dict[str, str]


class UploadLimits(BaseModel):
    max_bytes: int


class UploadSession(BaseModel):
    """``201`` body of ``POST {ws}/uploads``."""

    upload_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    expires_at: datetime
    upload: PresignedUpload
    limits: UploadLimits


class Retention(BaseModel):
    status: Literal["current", "superseded"]
    purge_after: datetime | None = None  # always null in M02


class Version(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    version_number: int
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_by: uuid.UUID
    created_at: datetime
    retention: Retention


class DocumentPermissions(BaseModel):
    write: bool
    manage: bool


class Document(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    version_count: int
    latest_version: Version | None
    permissions: DocumentPermissions
    # M03.03.04: processing state of the latest version (user-safe; ``none`` = no job yet).
    ingestion: IngestionSummary = IngestionSummary()


class DocumentList(BaseModel):
    items: list[Document]
    next_cursor: str | None
    can_upload: bool


class VersionList(BaseModel):
    items: list[Version]
    next_cursor: str | None


class DeletionAccepted(BaseModel):
    status: Literal["deletion_requested"] = "deletion_requested"
    purge_after: datetime


class ContentUrl(BaseModel):
    url: str
    expires_at: datetime
    filename: str
    content_type: str
    disposition: Disposition


class FinalizeResult(BaseModel):
    """``201`` (first time) / ``200`` (repeat) body of ``POST {ws}/uploads/{id}/finalize``."""

    document: Document
    version: Version
