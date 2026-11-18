"""Port-level contract for ``ObjectStore`` (M02.01.01 / M02.01.02).

Every test runs against ``S3ObjectStore`` (real RustFS) and ``InMemoryObjectStore`` unless it needs
real HTTP behaviour (``s3_only``). The "browser" is httpx for S3 and ``simulate_*`` for memory.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr
from storage_support import Browser, Stack

from app.infrastructure.object_storage.s3 import S3ObjectStore
from app.infrastructure.object_storage.settings import ObjectStoreSettings
from app.modules.storage.keys import original_key
from app.modules.storage.ports import (
    ChecksumMismatch,
    CompletedPart,
    InvalidMultipart,
    MultipartUploadNotFound,
    ObjectAlreadyExists,
    ObjectNotFound,
    ObjectStore,
    StorageUnavailable,
)

MIB = 1024 * 1024


def b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def tamper(url: str) -> str:
    """Flip the last character of the signature (always the last query parameter)."""
    return url[:-1] + ("0" if url[-1] != "0" else "1")


def read_all(store: ObjectStore, key: str) -> bytes:
    return b"".join(store.get_object(key))


# ------------------------------------------------------------------------------ keys


def test_original_key_rejects_non_uuid() -> None:
    with pytest.raises(ValueError, match="UUID"):
        original_key("../../x", uuid.uuid4(), uuid.uuid4())
    with pytest.raises(ValueError, match="UUID"):
        original_key(uuid.uuid4(), uuid.uuid4(), "report.pdf")


# ------------------------------------------------------------------------ put / head / get


def test_put_with_right_checksum_then_head_and_get(store: ObjectStore, key: str) -> None:
    data = os.urandom(3 * MIB + 17)  # several 1 MiB chunks
    info = store.put_object(key, data, content_type="application/pdf", sha256_b64=b64(data))
    assert (info.key, info.size, info.content_type) == (key, len(data), "application/pdf")
    head = store.head_object(key)
    assert head is not None
    assert head.size == len(data)
    assert head.content_type == "application/pdf"
    assert head.checksum_sha256_b64 == b64(data)  # checksum round trip
    assert head.etag
    assert head.last_modified is not None
    assert read_all(store, key) == data


def test_put_with_wrong_checksum_is_rejected_and_stores_nothing(
    store: ObjectStore, key: str
) -> None:
    with pytest.raises(ChecksumMismatch):
        store.put_object(key, b"real bytes", content_type="text/plain", sha256_b64=b64(b"other"))
    assert store.head_object(key) is None


def test_put_twice_is_object_already_exists_and_keeps_the_first(
    store: ObjectStore, key: str
) -> None:
    store.put_object(key, b"first", content_type="text/plain", sha256_b64=b64(b"first"))
    with pytest.raises(ObjectAlreadyExists):
        store.put_object(key, b"second", content_type="text/plain", sha256_b64=b64(b"second"))
    assert read_all(store, key) == b"first"
    # Explicitly unconditional writes (not used for originals) may overwrite.
    store.put_object(
        key, b"third", content_type="text/plain", sha256_b64=b64(b"third"), if_none_match=False
    )
    assert read_all(store, key) == b"third"


def test_head_and_get_of_a_missing_key(store: ObjectStore, key: str) -> None:
    assert store.head_object(key) is None
    with pytest.raises(ObjectNotFound):
        store.get_object(key)


def test_delete_is_idempotent(store: ObjectStore, key: str) -> None:
    store.delete_object(key)  # never existed
    store.put_object(key, b"x", content_type="text/plain", sha256_b64=b64(b"x"))
    store.delete_object(key)
    assert store.head_object(key) is None
    store.delete_object(key)  # already gone
    with pytest.raises(ObjectNotFound):
        store.get_object(key)


def test_invalid_input_never_reaches_the_store(store: ObjectStore, key: str) -> None:
    with pytest.raises(ValueError, match="sha256_b64"):
        store.put_object(key, b"x", content_type="text/plain", sha256_b64="not-a-digest")
    with pytest.raises(ValueError, match="content type"):
        store.put_object(key, b"x", content_type="text plain", sha256_b64=b64(b"x"))
    assert store.head_object(key) is None


# --------------------------------------------------------------------------- presigned PUT


def test_presigned_put_like_a_browser(store: ObjectStore, browser: Browser, key: str) -> None:
    data = os.urandom(200_000)
    req = store.presign_put(
        key, content_type="application/pdf", sha256_b64=b64(data), expires_seconds=60
    )
    assert req.method == "PUT"
    assert req.headers == {"Content-Type": "application/pdf", "x-amz-checksum-sha256": b64(data)}
    assert req.expires_at > datetime.now(UTC)
    assert req.expires_at <= datetime.now(UTC) + timedelta(seconds=61)
    assert store.head_object(key) is None  # nothing before the browser uploads

    resp = browser.put(req, data)
    assert resp.status == 200, resp
    head = store.head_object(key)
    assert head is not None
    assert (head.size, head.content_type, head.checksum_sha256_b64) == (
        len(data),
        "application/pdf",
        b64(data),
    )
    assert read_all(store, key) == data


def test_presigned_put_with_a_different_content_type_is_rejected(
    store: ObjectStore, browser: Browser, key: str
) -> None:
    data = b"%PDF-1.7 not really"
    req = store.presign_put(
        key, content_type="application/pdf", sha256_b64=b64(data), expires_seconds=60
    )
    swapped = {**req.headers, "Content-Type": "text/html"}
    assert browser.put(req, data, swapped).status == 403
    assert store.head_object(key) is None


def test_presigned_put_with_a_body_not_matching_the_signed_checksum_is_rejected(
    store: ObjectStore, browser: Browser, key: str
) -> None:
    req = store.presign_put(
        key, content_type="text/plain", sha256_b64=b64(b"declared"), expires_seconds=60
    )
    resp = browser.put(req, b"a different body")
    assert resp.status in (400, 403), resp
    assert store.head_object(key) is None
    # A client that lies in the checksum header breaks the signature instead.
    forged = {**req.headers, "x-amz-checksum-sha256": b64(b"a different body")}
    assert browser.put(req, b"a different body", forged).status == 403
    assert store.head_object(key) is None
    # The declared body is still accepted afterwards.
    assert browser.put(req, b"declared").status == 200


def test_presigned_put_missing_a_signed_header_is_rejected(
    store: ObjectStore, browser: Browser, key: str
) -> None:
    req = store.presign_put(
        key, content_type="text/plain", sha256_b64=b64(b"x"), expires_seconds=60
    )
    assert browser.put(req, b"x", {"Content-Type": "text/plain"}).status in (400, 403)
    assert store.head_object(key) is None


def test_tampered_presigned_put_is_rejected(store: ObjectStore, browser: Browser, key: str) -> None:
    req = store.presign_put(
        key, content_type="text/plain", sha256_b64=b64(b"x"), expires_seconds=60
    )
    bad = type(req)(req.method, tamper(req.url), req.headers, req.expires_at)
    assert browser.put(bad, b"x").status == 403
    assert store.head_object(key) is None


# --------------------------------------------------------------------------- presigned GET


def test_presigned_get_returns_exact_bytes(store: ObjectStore, browser: Browser, key: str) -> None:
    data = os.urandom(100_000) + b"\x00\r\n\xff"
    store.put_object(key, data, content_type="application/pdf", sha256_b64=b64(data))
    url = store.presign_get(
        key, expires_seconds=60, filename="Quarterly report.pdf", content_type=None, inline=False
    )
    resp = browser.get(url)
    assert resp.status == 200
    assert resp.body == data


def test_presigned_get_disposition_and_content_type_override(
    store: ObjectStore, browser: Browser, key: str
) -> None:
    store.put_object(
        key, b"<html>x</html>", content_type="text/html", sha256_b64=b64(b"<html>x</html>")
    )
    attachment = browser.get(
        store.presign_get(
            key,
            expires_seconds=60,
            filename="Résumé 2026.html",
            content_type="application/octet-stream",
            inline=False,
        )
    )
    disposition = attachment.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''R%C3%A9sum%C3%A9%202026.html" in disposition
    assert attachment.headers["content-type"] == "application/octet-stream"

    inline = browser.get(
        store.presign_get(
            key, expires_seconds=60, filename="note.txt", content_type="text/plain", inline=True
        )
    )
    assert inline.headers["content-disposition"].startswith("inline;")
    assert inline.headers["content-type"] == "text/plain"

    # Without an override the stored type is served, still as an attachment.
    plain = browser.get(
        store.presign_get(
            key, expires_seconds=60, filename="a.html", content_type=None, inline=False
        )
    )
    assert plain.headers["content-type"].startswith("text/html")
    assert plain.headers["content-disposition"].startswith("attachment;")


def test_presigned_get_filename_cannot_inject_headers(
    store: ObjectStore, browser: Browser, key: str
) -> None:
    store.put_object(key, b"x", content_type="text/plain", sha256_b64=b64(b"x"))
    url = store.presign_get(
        key,
        expires_seconds=60,
        filename='../a"; filename=evil.exe\r\nSet-Cookie: x=1',
        content_type="text/plain",
        inline=False,
    )
    resp = browser.get(url)
    assert resp.status == 200
    disposition = resp.headers["content-disposition"]
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert "/" not in disposition
    assert "set-cookie" not in resp.headers


def test_tampered_presigned_get_is_denied(store: ObjectStore, browser: Browser, key: str) -> None:
    store.put_object(
        key, b"payload-9f3a1c", content_type="text/plain", sha256_b64=b64(b"payload-9f3a1c")
    )
    url = store.presign_get(key, expires_seconds=60, filename="a", content_type=None, inline=False)
    resp = browser.get(tamper(url))
    assert resp.status == 403
    assert b"payload-9f3a1c" not in resp.body


def test_a_url_issued_before_delete_is_dead_after_delete(
    store: ObjectStore, browser: Browser, key: str
) -> None:
    store.put_object(key, b"delete me", content_type="text/plain", sha256_b64=b64(b"delete me"))
    url = store.presign_get(key, expires_seconds=300, filename="a", content_type=None, inline=False)
    assert browser.get(url).status == 200
    store.delete_object(key)
    resp = browser.get(url)
    assert resp.status == 404
    assert b"delete me" not in resp.body


def test_expired_presigned_get_is_denied(
    s3_only: S3ObjectStore, key: str, http: httpx.Client
) -> None:
    store = s3_only
    store.put_object(key, b"short lived", content_type="text/plain", sha256_b64=b64(b"short lived"))
    url = store.presign_get(key, expires_seconds=1, filename="a", content_type=None, inline=False)
    time.sleep(3)
    resp = http.get(url)
    assert resp.status_code in (400, 403)  # AWS/R2: 403; some stores: 400
    assert b"short lived" not in resp.content


def test_anonymous_access_is_denied(
    s3_only: S3ObjectStore, stack: Stack, key: str, http: httpx.Client
) -> None:
    s3_only.put_object(key, b"private", content_type="text/plain", sha256_b64=b64(b"private"))
    base = stack.settings.S3_PUBLIC_ENDPOINT_URL
    bucket = stack.settings.S3_BUCKET_ORIGINALS
    assert http.get(f"{base}/{bucket}/{key}").status_code in (401, 403)
    assert http.get(f"{base}/{bucket}?list-type=2").status_code in (401, 403)
    assert http.put(f"{base}/{bucket}/{key}.anon", content=b"x").status_code in (401, 403)


# ------------------------------------------------------------------------------- multipart


def _upload_parts(
    store: ObjectStore, browser: Browser, key: str, upload_id: str, chunks: list[bytes]
) -> list[CompletedPart]:
    parts: list[CompletedPart] = []
    for number, chunk in enumerate(chunks, start=1):
        req = store.presign_upload_part(key, upload_id, number, expires_seconds=120)
        assert req.method == "PUT"
        assert req.headers == {}
        resp = browser.put(req, chunk)
        assert resp.status == 200, resp
        parts.append(CompletedPart(number, resp.headers["etag"].strip('"')))
    return parts


def test_multipart_create_presigned_parts_list_and_complete(
    store: ObjectStore, browser: Browser, key: str
) -> None:
    chunks = [os.urandom(5 * MIB), os.urandom(5 * MIB), os.urandom(MIB + 7)]
    upload = store.create_multipart(key, content_type="application/zip")
    assert upload.key == key
    assert upload.upload_id
    parts = _upload_parts(store, browser, key, upload.upload_id, chunks)

    listed = store.list_parts(key, upload.upload_id)
    assert [(p.part_number, p.size) for p in listed] == [
        (i + 1, len(c)) for i, c in enumerate(chunks)
    ]
    assert [p.etag for p in listed] == [p.etag for p in parts]

    info = store.complete_multipart(key, upload.upload_id, parts)
    whole = b"".join(chunks)
    assert info.size == len(whole)
    assert info.content_type == "application/zip"
    assert read_all(store, key) == whole
    with pytest.raises(MultipartUploadNotFound):  # the upload is finished
        store.list_parts(key, upload.upload_id)


def test_multipart_abort_discards_everything_and_is_idempotent(
    store: ObjectStore, browser: Browser, key: str
) -> None:
    upload = store.create_multipart(key, content_type="application/octet-stream")
    _upload_parts(store, browser, key, upload.upload_id, [os.urandom(5 * MIB)])
    assert len(store.list_parts(key, upload.upload_id)) == 1
    store.abort_multipart(key, upload.upload_id)
    with pytest.raises(MultipartUploadNotFound):
        store.list_parts(key, upload.upload_id)
    store.abort_multipart(key, upload.upload_id)  # second abort: not an error
    store.abort_multipart(key, "no-such-upload")
    assert store.head_object(key) is None


def test_multipart_complete_errors(store: ObjectStore, browser: Browser, key: str) -> None:
    with pytest.raises(MultipartUploadNotFound):
        store.complete_multipart(key, "no-such-upload", [CompletedPart(1, "e")])
    upload = store.create_multipart(key, content_type="application/octet-stream")
    parts = _upload_parts(store, browser, key, upload.upload_id, [os.urandom(MIB), os.urandom(MIB)])
    with pytest.raises(InvalidMultipart):  # out of order, rejected before the network
        store.complete_multipart(key, upload.upload_id, list(reversed(parts)))
    with pytest.raises(InvalidMultipart):  # wrong ETag
        store.complete_multipart(key, upload.upload_id, [CompletedPart(1, "0" * 32), parts[1]])
    with pytest.raises(InvalidMultipart):  # a non-last part under 5 MiB
        store.complete_multipart(key, upload.upload_id, parts)
    store.abort_multipart(key, upload.upload_id)
    assert store.head_object(key) is None


def test_multipart_part_bound_to_a_checksum(store: ObjectStore, browser: Browser, key: str) -> None:
    upload = store.create_multipart(key, content_type="application/octet-stream")
    chunk = os.urandom(5 * MIB)
    req = store.presign_upload_part(
        key, upload.upload_id, 1, expires_seconds=120, sha256_b64=b64(chunk)
    )
    assert req.headers == {"x-amz-checksum-sha256": b64(chunk)}
    assert browser.put(req, b"not the declared part").status in (400, 403)
    assert browser.put(req, chunk).status == 200
    store.abort_multipart(key, upload.upload_id)


# -------------------------------------------------------------------------------- CORS


def test_cors_preflight_from_the_web_origin_is_allowed(
    s3_only: S3ObjectStore, stack: Stack, key: str, http: httpx.Client
) -> None:
    req = s3_only.presign_put(
        key, content_type="text/plain", sha256_b64=b64(b"x"), expires_seconds=60
    )
    pre = http.request(
        "OPTIONS",
        req.url,
        headers={
            "Origin": stack.web_origin,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type,x-amz-checksum-sha256",
        },
    )
    assert pre.status_code in (200, 204), (pre.status_code, pre.text[:200])
    assert pre.headers["access-control-allow-origin"] == stack.web_origin
    assert "PUT" in pre.headers["access-control-allow-methods"].upper()

    put = http.put(req.url, content=b"x", headers={**req.headers, "Origin": stack.web_origin})
    assert put.status_code == 200
    assert put.headers["access-control-allow-origin"] == stack.web_origin


def test_cors_preflight_from_an_unknown_origin_is_not_allowed(
    s3_only: S3ObjectStore, key: str, http: httpx.Client
) -> None:
    req = s3_only.presign_put(
        key, content_type="text/plain", sha256_b64=b64(b"x"), expires_seconds=60
    )
    pre = http.request(
        "OPTIONS",
        req.url,
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "PUT"},
    )
    assert pre.headers.get("access-control-allow-origin") not in ("http://evil.example", "*")


# ----------------------------------------------------------------------------- failures


def test_unreachable_store_is_storage_unavailable() -> None:
    settings = ObjectStoreSettings(
        S3_ENDPOINT_URL="http://127.0.0.1:1",  # nothing listens here
        S3_PUBLIC_ENDPOINT_URL="http://127.0.0.1:1",
        S3_ACCESS_KEY_ID=SecretStr("k"),
        S3_SECRET_ACCESS_KEY=SecretStr("s"),
        S3_CONNECT_TIMEOUT_SECONDS=0.5,
        S3_MAX_ATTEMPTS=2,
    )
    dead = S3ObjectStore(settings)
    started = time.monotonic()
    with pytest.raises(StorageUnavailable):
        dead.head_object("k")
    with pytest.raises(StorageUnavailable):
        dead.put_object("k", b"x", content_type="text/plain", sha256_b64=b64(b"x"))
    assert time.monotonic() - started < 10  # bounded retries, no hang
    # Presigning is pure computation and keeps working.
    url = dead.presign_get("k", expires_seconds=60, filename="a", content_type=None, inline=False)
    assert parse_qs(urlparse(url).query)["X-Amz-Expires"] == ["60"]


def test_wrong_credentials_are_storage_unavailable(stack: Stack) -> None:
    bad = S3ObjectStore(
        ObjectStoreSettings(
            S3_ENDPOINT_URL=stack.settings.S3_ENDPOINT_URL,
            S3_PUBLIC_ENDPOINT_URL=stack.settings.S3_PUBLIC_ENDPOINT_URL,
            S3_ACCESS_KEY_ID=stack.settings.S3_ACCESS_KEY_ID,
            S3_SECRET_ACCESS_KEY=SecretStr("wrong"),
        )
    )
    with pytest.raises(StorageUnavailable) as exc:
        bad.head_object("k")
    assert "wrong" not in str(exc.value)
