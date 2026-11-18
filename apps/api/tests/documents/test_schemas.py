"""Schemas, filename sanitizer and validators (M02.02.01 shared types)."""

from __future__ import annotations

import base64
import hashlib
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.modules.documents.schemas import (
    DocumentUpdate,
    UploadCreate,
    default_title,
    normalize_content_type,
    sanitize_filename,
    sha256_hex_to_b64,
    validate_sha256,
)

SHA = "a" * 64


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "size_bytes": 10,
        "sha256": SHA,
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("  a   b \t c.txt ", "a b c.txt"),
        ("../../etc/passwd", "passwd"),
        ("C:\\fakepath\\notes.md", "notes.md"),
        ("dir/sub\\file.bin", "file.bin"),
        ("a\x00b\x1fc\x7f.txt", "abc.txt"),
        ("line1\nline2\r\n.txt", "line1 line2 .txt"),
        ("evil\u202egnp.exe", "evilgnp.exe"),
        ("nbsp\u00a0here\u2028x.txt", "nbsp here x.txt"),
        ("ünïcode-日本語.pdf", "ünïcode-日本語.pdf"),
        ("", ""),
        (".", ""),
        ("..", ""),
        ("...", ""),
        ("a/", ""),
        ("\x00\x01", ""),
        (".bashrc", ".bashrc"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_is_idempotent() -> None:
    once = sanitize_filename("  ../x\u202e y\t.txt ")
    assert sanitize_filename(once) == once


def test_upload_create_sanitizes_and_normalizes() -> None:
    model = UploadCreate.model_validate(
        _body(filename="  ../a   b.PDF ", content_type="Application/PDF", title="  T  ")
    )
    assert model.filename == "a b.PDF"
    assert model.content_type == "application/pdf"
    assert model.title == "T"
    assert model.document_id is None


def test_upload_create_with_document_id() -> None:
    doc = uuid.uuid4()
    assert UploadCreate.model_validate(_body(document_id=str(doc))).document_id == doc


@pytest.mark.parametrize(
    "overrides",
    [
        {"filename": ""},
        {"filename": ".."},
        {"filename": "x" * 256},
        {"filename": 5},
        {"content_type": "pdf"},
        {"content_type": "application/pdf; charset=x"},
        {"content_type": "a/b/c"},
        {"content_type": "x/" + "y" * 255},
        {"content_type": ""},
        {"size_bytes": 0},
        {"size_bytes": -1},
        {"size_bytes": "ten"},
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"sha256": "g" * 64},
        {"sha256": "a" * 64 + "\n"},
        {"document_id": "not-a-uuid"},
        {"title": ""},
        {"title": "   "},
        {"title": "t" * 256},
        {"title": "bad\x00title"},
        {"extra": 1},
    ],
)
def test_upload_create_rejects(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        UploadCreate.model_validate(_body(**overrides))


def test_filename_boundary_255_accepted() -> None:
    assert len(UploadCreate.model_validate(_body(filename="x" * 255)).filename) == 255


def test_title_boundary_255_accepted() -> None:
    assert len(UploadCreate.model_validate(_body(title="t" * 255)).title or "") == 255


@pytest.mark.parametrize("missing", ["filename", "content_type", "size_bytes", "sha256"])
def test_upload_create_requires_fields(missing: str) -> None:
    body = _body()
    del body[missing]
    with pytest.raises(ValidationError):
        UploadCreate.model_validate(body)


def test_document_update_forbids_extra_and_trims() -> None:
    assert DocumentUpdate.model_validate({"title": "  New  "}).title == "New"
    with pytest.raises(ValidationError):
        DocumentUpdate.model_validate({"title": "x", "owner": "me"})
    with pytest.raises(ValidationError):
        DocumentUpdate.model_validate({"title": " "})
    with pytest.raises(ValidationError):
        DocumentUpdate.model_validate({})


@pytest.mark.parametrize(
    "value",
    ["text/plain", "application/vnd.ms-excel", "image/svg+xml", "application/x-7z-compressed"],
)
def test_content_type_ok(value: str) -> None:
    assert normalize_content_type(value) == value


@pytest.mark.parametrize("value", ["", "text", "text/", "/plain", "te xt/plain", "text/pl ain"])
def test_content_type_bad(value: str) -> None:
    with pytest.raises(ValueError, match="type/subtype"):
        normalize_content_type(value)


def test_sha256_validator_and_base64() -> None:
    digest = hashlib.sha256(b"hello").hexdigest()
    assert validate_sha256(digest) == digest
    assert sha256_hex_to_b64(digest) == base64.b64encode(hashlib.sha256(b"hello").digest()).decode()
    for bad in ("", digest.upper(), digest[:-1], digest + "0"):
        with pytest.raises(ValueError, match="sha256"):
            validate_sha256(bad)


@pytest.mark.parametrize(
    ("filename", "title"),
    [
        ("report.pdf", "report"),
        ("archive.tar.gz", "archive.tar"),
        ("noext", "noext"),
        (".bashrc", ".bashrc"),
        ("trailing.", "trailing"),
        ("a b .txt", "a b"),
    ],
)
def test_default_title(filename: str, title: str) -> None:
    assert default_title(filename) == title
