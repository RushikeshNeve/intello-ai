"""Unit tests: S3ObjectStore request shape and botocore error mapping (stubbed, no network)."""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import (
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.stub import Stubber
from pydantic import SecretStr

from app.infrastructure.object_storage.s3 import S3ObjectStore
from app.infrastructure.object_storage.settings import ObjectStoreSettings
from app.modules.storage.ports import (
    ChecksumMismatch,
    CompletedPart,
    InvalidMultipart,
    MultipartUploadNotFound,
    ObjectAlreadyExists,
    ObjectNotFound,
    StorageUnavailable,
)

SECRET = "super-secret-value"
ACCESS = "AKIATESTKEY"
BUCKET = "ukos-originals"
DATA = b"hello"
SHA = base64.b64encode(hashlib.sha256(DATA).digest()).decode()


def _settings(**kw: Any) -> ObjectStoreSettings:
    return ObjectStoreSettings(
        S3_ACCESS_KEY_ID=SecretStr(ACCESS),
        S3_SECRET_ACCESS_KEY=SecretStr(SECRET),
        S3_ENDPOINT_URL="http://objectstore:9000",
        S3_PUBLIC_ENDPOINT_URL="http://127.0.0.1:39301",
        **kw,
    )


def _client(endpoint: str) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=ACCESS,
        aws_secret_access_key=SECRET,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"total_max_attempts": 1},
        ),
    )


@pytest.fixture
def stubbed() -> tuple[S3ObjectStore, Stubber]:
    client = _client("http://objectstore:9000")
    stubber = Stubber(client)
    stubber.activate()
    store = S3ObjectStore(
        _settings(), client=client, presign_client=_client("http://127.0.0.1:39301")
    )
    return store, stubber


class _Raiser:
    """A client whose every operation raises ``exc``."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __getattr__(self, name: str) -> Any:
        def call(**_: Any) -> Any:
            raise self._exc

        return call


def _store_raising(exc: Exception) -> S3ObjectStore:
    return S3ObjectStore(_settings(), client=_Raiser(exc), presign_client=_client("http://x:1"))


# ------------------------------------------------------------------ ClientError mapping


def test_put_412_is_already_exists(stubbed: tuple[S3ObjectStore, Stubber]) -> None:
    store, stub = stubbed
    stub.add_client_error("put_object", "PreconditionFailed", http_status_code=412)
    with pytest.raises(ObjectAlreadyExists):
        store.put_object("k", DATA, content_type="text/plain", sha256_b64=SHA)


def test_put_bad_digest_is_checksum_mismatch(stubbed: tuple[S3ObjectStore, Stubber]) -> None:
    store, stub = stubbed
    stub.add_client_error("put_object", "BadDigest", http_status_code=400)
    with pytest.raises(ChecksumMismatch):
        store.put_object("k", DATA, content_type="text/plain", sha256_b64=SHA)


def test_put_sends_checksum_and_if_none_match(stubbed: tuple[S3ObjectStore, Stubber]) -> None:
    store, stub = stubbed
    stub.add_response(
        "put_object",
        {"ETag": '"abc"'},
        {
            "Bucket": BUCKET,
            "Key": "k",
            "Body": DATA,
            "ContentType": "text/plain",
            "ChecksumSHA256": SHA,
            "IfNoneMatch": "*",
        },
    )
    info = store.put_object("k", DATA, content_type="text/plain", sha256_b64=SHA)
    assert (info.size, info.etag, info.checksum_sha256_b64) == (5, "abc", SHA)
    stub.assert_no_pending_responses()


def test_put_without_condition(stubbed: tuple[S3ObjectStore, Stubber]) -> None:
    store, stub = stubbed
    stub.add_response(
        "put_object",
        {"ETag": '"abc"'},
        {
            "Bucket": BUCKET,
            "Key": "k",
            "Body": DATA,
            "ContentType": "text/plain",
            "ChecksumSHA256": SHA,
        },
    )
    store.put_object("k", DATA, content_type="text/plain", sha256_b64=SHA, if_none_match=False)
    stub.assert_no_pending_responses()


def test_put_rejects_bad_checksum_input_locally() -> None:
    store = _store_raising(AssertionError("must not call the network"))
    with pytest.raises(ValueError, match="sha256_b64"):
        store.put_object("k", DATA, content_type="text/plain", sha256_b64="deadbeef")


def test_head_missing_returns_none_and_found_reads_checksum(
    stubbed: tuple[S3ObjectStore, Stubber],
) -> None:
    store, stub = stubbed
    stub.add_client_error("head_object", "404", http_status_code=404)
    assert store.head_object("k") is None
    stub.add_response(
        "head_object",
        {"ContentLength": 5, "ContentType": "text/plain", "ChecksumSHA256": SHA, "ETag": '"e"'},
        {"Bucket": BUCKET, "Key": "k", "ChecksumMode": "ENABLED"},
    )
    info = store.head_object("k")
    assert info is not None
    assert (info.size, info.content_type, info.checksum_sha256_b64, info.etag) == (
        5,
        "text/plain",
        SHA,
        "e",
    )


def test_get_missing_is_object_not_found_before_iteration(
    stubbed: tuple[S3ObjectStore, Stubber],
) -> None:
    store, stub = stubbed
    stub.add_client_error("get_object", "NoSuchKey", http_status_code=404)
    with pytest.raises(ObjectNotFound):
        store.get_object("k")


def test_delete_missing_is_idempotent(stubbed: tuple[S3ObjectStore, Stubber]) -> None:
    store, stub = stubbed
    stub.add_client_error("delete_object", "NoSuchKey", http_status_code=404)
    store.delete_object("k")


def test_server_errors_are_unavailable(stubbed: tuple[S3ObjectStore, Stubber]) -> None:
    store, stub = stubbed
    stub.add_client_error("delete_object", "InternalError", http_status_code=500)
    with pytest.raises(StorageUnavailable):
        store.delete_object("k")
    stub.add_client_error("head_object", "403", http_status_code=403)
    with pytest.raises(
        StorageUnavailable
    ):  # rejected credentials are a server-side misconfiguration
        store.head_object("k")
    stub.add_client_error("put_object", "NoSuchBucket", http_status_code=404)
    with pytest.raises(StorageUnavailable):
        store.put_object("k", DATA, content_type="text/plain", sha256_b64=SHA)


def test_multipart_error_mapping(stubbed: tuple[S3ObjectStore, Stubber]) -> None:
    store, stub = stubbed
    stub.add_client_error("list_parts", "NoSuchUpload", http_status_code=404)
    with pytest.raises(MultipartUploadNotFound):
        store.list_parts("k", "u")
    stub.add_client_error("abort_multipart_upload", "NoSuchUpload", http_status_code=404)
    store.abort_multipart("k", "u")  # idempotent
    stub.add_client_error("complete_multipart_upload", "InvalidPart", http_status_code=400)
    with pytest.raises(InvalidMultipart):
        store.complete_multipart("k", "u", [CompletedPart(1, "e")])
    stub.add_client_error("complete_multipart_upload", "NoSuchUpload", http_status_code=404)
    with pytest.raises(MultipartUploadNotFound):
        store.complete_multipart("k", "u", [CompletedPart(1, "e")])


def test_complete_rejects_unordered_parts_locally() -> None:
    store = _store_raising(AssertionError("must not call the network"))
    for parts in ([], [CompletedPart(2, "a"), CompletedPart(1, "b")], [CompletedPart(1, "a")] * 2):
        with pytest.raises(InvalidMultipart):
            store.complete_multipart("k", "u", parts)


# ------------------------------------------------------------------ transport errors


@pytest.mark.parametrize(
    "exc",
    [
        ConnectTimeoutError(endpoint_url=f"http://u:{SECRET}@host:9000"),
        ReadTimeoutError(endpoint_url="http://host:9000"),
        EndpointConnectionError(endpoint_url="http://host:9000"),
    ],
)
def test_timeouts_and_connection_errors_are_unavailable(exc: Exception) -> None:
    store = _store_raising(exc)
    calls: list[Callable[[], object]] = [
        lambda: store.put_object("k", DATA, content_type="text/plain", sha256_b64=SHA),
        lambda: store.head_object("k"),
        lambda: store.get_object("k"),
        lambda: store.delete_object("k"),
        lambda: store.create_multipart("k", content_type="text/plain"),
        lambda: store.complete_multipart("k", "u", [CompletedPart(1, "e")]),
        lambda: store.abort_multipart("k", "u"),
        lambda: store.list_parts("k", "u"),
    ]
    for call in calls:
        with pytest.raises(StorageUnavailable) as raised:
            call()
        text = repr(raised.value) + str(raised.value)
        assert SECRET not in text
        assert ACCESS not in text
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__


def test_failures_are_logged_without_secrets(caplog: pytest.LogCaptureFixture) -> None:
    store = _store_raising(ConnectTimeoutError(endpoint_url=f"http://u:{SECRET}@host:9000"))
    with caplog.at_level(logging.DEBUG, logger="ukos.storage"), pytest.raises(StorageUnavailable):
        store.head_object("k")
    assert caplog.records
    assert SECRET not in caplog.text
    assert ACCESS not in caplog.text


def test_interrupted_stream_is_unavailable() -> None:
    class Body:
        closed = False

        def iter_chunks(self, _: int) -> Any:
            yield b"part"
            raise ReadTimeoutError(endpoint_url="http://host:9000")

        def close(self) -> None:
            self.closed = True

    body = Body()
    store = S3ObjectStore(
        _settings(),
        client=type("C", (), {"get_object": lambda self, **_: {"Body": body}})(),
        presign_client=_client("http://x:1"),
    )
    stream = store.get_object("k")
    assert next(stream) == b"part"
    with pytest.raises(StorageUnavailable):
        next(stream)
    assert body.closed


# ------------------------------------------------------------------ presigning (offline)


def test_presign_put_signs_content_type_and_checksum_against_the_public_endpoint(
    stubbed: tuple[S3ObjectStore, Stubber],
) -> None:
    store, _ = stubbed
    req = store.presign_put(
        "org/a/b", content_type="application/pdf", sha256_b64=SHA, expires_seconds=60
    )
    parts = urlparse(req.url)
    assert (parts.scheme, parts.netloc, parts.path) == (
        "http",
        "127.0.0.1:39301",
        f"/{BUCKET}/org/a/b",
    )
    qs = parse_qs(parts.query)
    assert qs["X-Amz-SignedHeaders"] == ["content-type;host;x-amz-checksum-sha256"]
    assert qs["X-Amz-Expires"] == ["60"]
    assert req.method == "PUT"
    assert req.headers == {"Content-Type": "application/pdf", "x-amz-checksum-sha256": SHA}
    assert SECRET not in req.url
    assert "signature" not in repr(req).lower()
    assert "X-Amz-Signature" not in repr(req)


def test_presign_get_signs_disposition_and_type(stubbed: tuple[S3ObjectStore, Stubber]) -> None:
    store, _ = stubbed
    url = store.presign_get(
        "k",
        expires_seconds=60,
        filename="../Résumé.pdf",
        content_type="application/pdf",
        inline=False,
    )
    qs = parse_qs(urlparse(url).query)
    assert qs["response-content-disposition"][0].startswith("attachment; filename=")
    assert "filename*=UTF-8''R%C3%A9sum%C3%A9.pdf" in qs["response-content-disposition"][0]
    assert qs["response-content-type"] == ["application/pdf"]
    inline = store.presign_get(
        "k", expires_seconds=60, filename="a.png", content_type="image/png", inline=True
    )
    assert parse_qs(urlparse(inline).query)["response-content-disposition"][0].startswith("inline;")
    bad_type = store.presign_get(
        "k", expires_seconds=60, filename="a", content_type="text/html\r\nX: y", inline=False
    )
    assert parse_qs(urlparse(bad_type).query)["response-content-type"] == [
        "application/octet-stream"
    ]
    no_override = store.presign_get(
        "k", expires_seconds=60, filename="a", content_type=None, inline=False
    )
    assert "response-content-type" not in parse_qs(urlparse(no_override).query)


def test_presign_validates_inputs(stubbed: tuple[S3ObjectStore, Stubber]) -> None:
    store, _ = stubbed
    with pytest.raises(ValueError, match="expires_seconds"):
        store.presign_put("k", content_type="a/b", sha256_b64=SHA, expires_seconds=0)
    with pytest.raises(ValueError, match="sha256_b64"):
        store.presign_put("k", content_type="a/b", sha256_b64="x", expires_seconds=5)
    with pytest.raises(ValueError, match="content type"):
        store.presign_put("k", content_type="a b", sha256_b64=SHA, expires_seconds=5)
    with pytest.raises(ValueError, match="part_number"):
        store.presign_upload_part("k", "u", 0, expires_seconds=5)
