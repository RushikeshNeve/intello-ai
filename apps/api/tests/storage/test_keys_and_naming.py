"""Unit tests: object keys and the pure header/validation helpers (no network)."""

from __future__ import annotations

import base64
import hashlib
import uuid
from urllib.parse import unquote

import pytest

from app.modules.storage.keys import original_key, parse_original_key
from app.modules.storage.naming import (
    content_disposition,
    sanitize_filename,
    validate_content_type,
    validate_expiry,
    validate_part_number,
    validate_sha256_b64,
)

ORG, DOC, VER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_original_key_shape_and_roundtrip() -> None:
    key = original_key(ORG, DOC, VER)
    assert key == f"org/{ORG}/doc/{DOC}/{VER}/original"
    assert original_key(str(ORG), str(DOC), str(VER)) == key
    assert tuple(parse_original_key(key)) == (ORG, DOC, VER)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-a-uuid",
        "../../etc/passwd",
        f"{uuid.uuid4()}/../x",
        str(uuid.uuid4()).upper(),
        str(uuid.uuid4()).replace("-", ""),
        f"{{{uuid.uuid4()}}}",
        f"urn:uuid:{uuid.uuid4()}",
        f" {uuid.uuid4()}",
        f"{uuid.uuid4()}\n",
        f"{uuid.uuid4()}/original",
    ],
)
@pytest.mark.parametrize("position", [0, 1, 2])
def test_original_key_rejects_non_uuid(bad: str, position: int) -> None:
    args: list[object] = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    args[position] = bad
    with pytest.raises(ValueError, match="UUID"):
        original_key(*args)  # type: ignore[arg-type]


def test_original_key_rejects_non_string_types() -> None:
    with pytest.raises(ValueError, match="UUID"):
        original_key(1, DOC, VER)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "key",
    [
        "",
        f"org/{ORG}/doc/{DOC}/{VER}",
        f"org/{ORG}/doc/{DOC}/{VER}/original/extra",
        f"org/{ORG}/doc/{DOC}/{VER}/report.pdf",
        f"org/{str(ORG).upper()}/doc/{DOC}/{VER}/original",
        f"org/{ORG}/doc/{DOC}/{'z' * 36}/original",
        f"prefix/org/{ORG}/doc/{DOC}/{VER}/original",
    ],
)
def test_parse_original_key_rejects_foreign_keys(key: str) -> None:
    with pytest.raises(ValueError, match="original object key"):
        parse_original_key(key)


# ----------------------------------------------------------------------------- naming


def test_sanitize_filename() -> None:
    assert sanitize_filename("report.pdf") == "report.pdf"
    assert sanitize_filename("../../etc/passwd") == "etc passwd"
    assert sanitize_filename("a\\b/c.txt") == "a b c.txt"
    assert sanitize_filename("  many   spaces\t\nhere.txt ") == "many spaces here.txt"
    assert sanitize_filename("evil\x00\x1f\x7f.txt") == "evil.txt"
    assert sanitize_filename("rtl‮txt.exe") == "rtltxt.exe"  # bidi override removed
    assert sanitize_filename("") == "download"
    assert sanitize_filename("///") == "download"
    assert sanitize_filename(".hidden") == "hidden"
    assert len(sanitize_filename("x" * 1000)) == 255


def test_content_disposition_attachment_and_inline() -> None:
    assert content_disposition("a.pdf", inline=False).startswith("attachment; ")
    assert content_disposition("a.pdf", inline=True).startswith("inline; ")
    value = content_disposition('Résumé "final"; v2.pdf', inline=False)
    assert 'filename="R_sum_ _final__ v2.pdf"' in value or 'filename="R_sum_' in value
    star = value.split("filename*=UTF-8''", 1)[1]
    assert unquote(star) == 'Résumé "final"; v2.pdf'
    assert all(c not in star for c in ' ";\\')  # only RFC 5987 attr-chars and %XX
    # header injection is impossible: no CR/LF survives
    injected = content_disposition("a\r\nSet-Cookie: x=1", inline=False)
    assert "\r" not in injected
    assert "\n" not in injected


def test_validate_sha256_b64() -> None:
    good = base64.b64encode(hashlib.sha256(b"x").digest()).decode()
    assert validate_sha256_b64(good) == good
    for bad in ["", "abc", hashlib.sha256(b"x").hexdigest(), good[:-1], good + "=", "!" * 44]:
        with pytest.raises(ValueError, match="sha256_b64"):
            validate_sha256_b64(bad)


def test_validate_content_type_and_ranges() -> None:
    assert validate_content_type("application/pdf") == "application/pdf"
    assert validate_content_type("text/plain; charset=utf-8") == "text/plain; charset=utf-8"
    for bad in ["", "pdf", "text/", "a/b\r\nX: y", "text/plain\n", "te xt/plain"]:
        with pytest.raises(ValueError, match="content type"):
            validate_content_type(bad)
    assert validate_expiry(300) == 300
    for bad_exp in (0, -1, 7 * 24 * 3600 + 1):
        with pytest.raises(ValueError, match="expires_seconds"):
            validate_expiry(bad_exp)
    assert validate_part_number(10_000) == 10_000
    for bad_part in (0, 10_001):
        with pytest.raises(ValueError, match="part_number"):
            validate_part_number(bad_part)
