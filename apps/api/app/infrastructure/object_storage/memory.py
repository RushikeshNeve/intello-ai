"""In-memory ``ObjectStore`` for fast tests and for other modules' API tests (M02.01.02).

It follows the port's semantics (conditional create, checksum verification, idempotent delete,
multipart rules) and returns deterministic fake presigned URLs::

    http://memory.invalid/<key>?op=put&exp=<epoch>&ct=<content-type>&sum=<sha256_b64>&sig=<hex>

To emulate the *browser* use ``simulate_upload`` (presigned PUT of the last request issued for a
key), ``simulate_put`` (any issued ``PresignedRequest``, incl. multipart parts) and ``simulate_get``
(any presigned GET URL). They answer like the real store: 200, 400 (checksum), 403 (not issued,
tampered, expired or headers differ from the signed ones), 404 (deleted). For tests only.
"""

from __future__ import annotations

import base64
import hashlib
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

from app.modules.storage.naming import (
    content_disposition,
    is_valid_content_type,
    validate_content_type,
    validate_expiry,
    validate_part_number,
    validate_sha256_b64,
)
from app.modules.storage.ports import (
    MULTIPART_LIMITS,
    ChecksumMismatch,
    CompletedPart,
    InvalidMultipart,
    MultipartUpload,
    MultipartUploadNotFound,
    ObjectAlreadyExists,
    ObjectInfo,
    ObjectNotFound,
    PartInfo,
    PresignedRequest,
)

BASE_URL = "http://memory.invalid"
_CHUNK = 64 * 1024


def _b64_sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


@dataclass(slots=True)
class _Object:
    data: bytes
    content_type: str | None
    checksum: str | None
    etag: str
    last_modified: datetime


@dataclass(slots=True)
class _Upload:
    key: str
    content_type: str
    parts: dict[int, tuple[bytes, str, str | None]] = field(
        default_factory=dict
    )  # n -> data, etag, sha


@dataclass(frozen=True, slots=True)
class _Issued:
    op: str  # put | part | get
    key: str
    expires_at: datetime
    headers: dict[str, str]
    upload_id: str | None = None
    part_number: int | None = None
    disposition: str | None = None
    response_content_type: str | None = None


@dataclass(frozen=True, slots=True)
class SimulatedResponse:
    """What the object store would answer to a browser request."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


class SimulatedRequestRejected(Exception):
    """``simulate_upload`` was refused with a 4xx other than a checksum mismatch."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"{status}: {reason}")
        self.status = status
        self.reason = reason


class InMemoryObjectStore:
    """Thread-safe dict-backed implementation of ``ObjectStore``.

    ``clock`` (default: ``datetime.now(UTC)``) lets tests move time to expire presigned URLs.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._objects: dict[str, _Object] = {}
        self._uploads: dict[str, _Upload] = {}
        self._issued: dict[str, _Issued] = {}
        self._last_put: dict[str, str] = {}  # key -> url of the last presigned PUT

    # ------------------------------------------------------------------ introspection (tests)

    @property
    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._objects)

    def read(self, key: str) -> bytes:
        """The stored bytes (test helper; raises ``ObjectNotFound``)."""
        with self._lock:
            obj = self._objects.get(key)
            if obj is None:
                raise ObjectNotFound("read: not found")
            return obj.data

    # ------------------------------------------------------------------ objects

    def _store(self, key: str, data: bytes, content_type: str | None, checksum: str) -> _Object:
        obj = _Object(
            data=data,
            content_type=content_type,
            checksum=checksum,
            etag=hashlib.md5(data, usedforsecurity=False).hexdigest(),
            last_modified=self._clock().replace(microsecond=0),
        )
        self._objects[key] = obj
        return obj

    @staticmethod
    def _info(key: str, obj: _Object) -> ObjectInfo:
        return ObjectInfo(
            key=key,
            size=len(obj.data),
            content_type=obj.content_type,
            checksum_sha256_b64=obj.checksum,
            etag=obj.etag,
            last_modified=obj.last_modified,
        )

    def put_object(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        sha256_b64: str,
        if_none_match: bool = True,
    ) -> ObjectInfo:
        validate_sha256_b64(sha256_b64)
        content_type = validate_content_type(content_type)
        if _b64_sha256(data) != sha256_b64:
            raise ChecksumMismatch("put_object: checksum mismatch (BadDigest)")
        with self._lock:
            if if_none_match and key in self._objects:
                raise ObjectAlreadyExists("put_object: object already exists")
            return self._info(key, self._store(key, data, content_type, sha256_b64))

    def head_object(self, key: str) -> ObjectInfo | None:
        with self._lock:
            obj = self._objects.get(key)
            return None if obj is None else self._info(key, obj)

    def get_object(self, key: str) -> Iterator[bytes]:
        with self._lock:
            obj = self._objects.get(key)
            if obj is None:
                raise ObjectNotFound("get_object: not found")
            data = obj.data
        return iter([data[i : i + _CHUNK] for i in range(0, len(data), _CHUNK)])

    def delete_object(self, key: str) -> None:
        with self._lock:
            self._objects.pop(key, None)

    # ------------------------------------------------------------------ presigned URLs

    def _issue(self, issued: _Issued, query: dict[str, str]) -> str:
        canonical = urlencode(sorted(query.items()))
        sig = hashlib.sha256(f"{issued.key}?{canonical}".encode()).hexdigest()
        url = f"{BASE_URL}/{quote(issued.key, safe='/')}?{canonical}&sig={sig}"
        with self._lock:
            self._issued[url] = issued
            if issued.op == "put":
                self._last_put[issued.key] = url
        return url

    def presign_put(
        self, key: str, *, content_type: str, sha256_b64: str, expires_seconds: int
    ) -> PresignedRequest:
        validate_sha256_b64(sha256_b64)
        content_type = validate_content_type(content_type)
        expires = validate_expiry(expires_seconds)
        expires_at = self._clock() + timedelta(seconds=expires)
        headers = {"Content-Type": content_type, "x-amz-checksum-sha256": sha256_b64}
        url = self._issue(
            _Issued("put", key, expires_at, headers),
            {
                "op": "put",
                "exp": str(int(expires_at.timestamp())),
                "ct": content_type,
                "sum": sha256_b64,
            },
        )
        return PresignedRequest("PUT", url, headers, expires_at)

    def presign_get(
        self,
        key: str,
        *,
        expires_seconds: int,
        filename: str,
        content_type: str | None,
        inline: bool,
    ) -> str:
        expires = validate_expiry(expires_seconds)
        expires_at = self._clock() + timedelta(seconds=expires)
        disposition = content_disposition(filename, inline=inline)
        response_type = None
        if content_type is not None:
            response_type = (
                content_type if is_valid_content_type(content_type) else "application/octet-stream"
            )
        query = {"op": "get", "exp": str(int(expires_at.timestamp())), "rcd": disposition}
        if response_type:
            query["rct"] = response_type
        return self._issue(
            _Issued(
                "get",
                key,
                expires_at,
                {},
                disposition=disposition,
                response_content_type=response_type,
            ),
            query,
        )

    # ------------------------------------------------------------------ multipart

    def create_multipart(self, key: str, *, content_type: str) -> MultipartUpload:
        content_type = validate_content_type(content_type)
        upload_id = uuid.uuid4().hex
        with self._lock:
            self._uploads[upload_id] = _Upload(key=key, content_type=content_type)
        return MultipartUpload(key=key, upload_id=upload_id)

    def _upload(self, key: str, upload_id: str) -> _Upload:
        upload = self._uploads.get(upload_id)
        if upload is None or upload.key != key:
            raise MultipartUploadNotFound("no such multipart upload")
        return upload

    def presign_upload_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        *,
        expires_seconds: int,
        sha256_b64: str | None = None,
    ) -> PresignedRequest:
        validate_part_number(part_number)
        expires = validate_expiry(expires_seconds)
        headers: dict[str, str] = {}
        if sha256_b64 is not None:
            headers["x-amz-checksum-sha256"] = validate_sha256_b64(sha256_b64)
        expires_at = self._clock() + timedelta(seconds=expires)
        query = {
            "op": "part",
            "exp": str(int(expires_at.timestamp())),
            "uploadId": upload_id,
            "partNumber": str(part_number),
        }
        if sha256_b64 is not None:
            query["sum"] = sha256_b64
        url = self._issue(
            _Issued("part", key, expires_at, headers, upload_id=upload_id, part_number=part_number),
            query,
        )
        return PresignedRequest("PUT", url, headers, expires_at)

    def complete_multipart(
        self, key: str, upload_id: str, parts: Sequence[CompletedPart]
    ) -> ObjectInfo:
        numbers = [p.part_number for p in parts]
        if not parts or numbers != sorted(set(numbers)):
            raise InvalidMultipart("complete_multipart: parts must be unique and ascending")
        with self._lock:
            upload = self._upload(key, upload_id)
            chunks: list[bytes] = []
            for i, wanted in enumerate(parts):
                stored = upload.parts.get(wanted.part_number)
                if stored is None or stored[1] != wanted.etag.strip('"'):
                    raise InvalidMultipart("complete_multipart: invalid part list (InvalidPart)")
                if i < len(parts) - 1 and len(stored[0]) < MULTIPART_LIMITS.min_part_bytes:
                    raise InvalidMultipart("complete_multipart: invalid part list (EntityTooSmall)")
                chunks.append(stored[0])
            data = b"".join(chunks)
            obj = self._store(key, data, upload.content_type, _b64_sha256(data))
            # Like S3: a multipart object has no whole-object SHA-256 and a composite ETag.
            obj.checksum = None
            obj.etag = f"{uuid.uuid4().hex}-{len(parts)}"
            del self._uploads[upload_id]
            return self._info(key, obj)

    def abort_multipart(self, key: str, upload_id: str) -> None:
        with self._lock:
            upload = self._uploads.get(upload_id)
            if upload is not None and upload.key == key:
                del self._uploads[upload_id]

    def list_parts(self, key: str, upload_id: str) -> list[PartInfo]:
        with self._lock:
            upload = self._upload(key, upload_id)
            return [
                PartInfo(n, etag, len(data)) for n, (data, etag, _) in sorted(upload.parts.items())
            ]

    # ------------------------------------------------------------------ browser emulation

    def _check(self, url: str, op: str, headers: Mapping[str, str]) -> _Issued | SimulatedResponse:
        issued = self._issued.get(url)
        if issued is None or issued.op != op:
            return SimulatedResponse(403)  # unknown or tampered URL
        if self._clock() >= issued.expires_at:
            return SimulatedResponse(403)  # expired
        sent = {k.lower(): v for k, v in headers.items()}
        for name, value in issued.headers.items():
            if sent.get(name.lower()) != value:
                return SimulatedResponse(403)  # signed header missing or different
        return issued

    def simulate_put(
        self, request: PresignedRequest, data: bytes, headers: Mapping[str, str] | None = None
    ) -> SimulatedResponse:
        """Emulate the browser PUT of a presigned object or part upload.

        ``headers`` default to the request's own headers (a well-behaved browser).
        """
        sent = request.headers if headers is None else headers
        with self._lock:
            checked = self._check(request.url, "put", sent)
            if isinstance(checked, SimulatedResponse):
                checked = self._check(request.url, "part", sent)
            if isinstance(checked, SimulatedResponse):
                return checked
            issued = checked
            sums = {k.lower(): v for k, v in sent.items()}.get("x-amz-checksum-sha256")
            if sums is not None and _b64_sha256(data) != sums:
                return SimulatedResponse(400, body=b"BadDigest")
            if issued.op == "part":
                upload = self._uploads.get(issued.upload_id or "")
                if upload is None or issued.part_number is None:
                    return SimulatedResponse(404, body=b"NoSuchUpload")
                etag = hashlib.md5(data, usedforsecurity=False).hexdigest()
                upload.parts[issued.part_number] = (data, etag, sums)
                return SimulatedResponse(200, {"etag": f'"{etag}"'})
            ct = {k.lower(): v for k, v in sent.items()}.get("content-type")
            obj = self._store(issued.key, data, ct, _b64_sha256(data))
            return SimulatedResponse(200, {"etag": f'"{obj.etag}"'})

    def simulate_upload(self, key: str, data: bytes, content_type: str) -> ObjectInfo:
        """Browser PUT to the last presigned PUT issued for ``key``, sending exactly its headers.

        Raises ``ChecksumMismatch`` (400) when ``data`` is not the body that was signed, and
        ``SimulatedRequestRejected`` for other refusals (no or expired URL, other content type).
        """
        with self._lock:
            url = self._last_put.get(key)
            issued = self._issued.get(url) if url else None
        if url is None or issued is None:
            raise SimulatedRequestRejected(403, "no presigned PUT was issued for this key")
        headers = dict(issued.headers)
        headers["Content-Type"] = content_type
        request = PresignedRequest("PUT", url, dict(issued.headers), issued.expires_at)
        response = self.simulate_put(request, data, headers)
        if response.status == 400:
            raise ChecksumMismatch("simulate_upload: checksum mismatch (BadDigest)")
        if response.status != 200:
            raise SimulatedRequestRejected(response.status, "request does not match the signature")
        info = self.head_object(key)
        assert info is not None  # noqa: S101
        return info

    def simulate_get(self, url: str) -> SimulatedResponse:
        """Emulate the browser GET of a presigned URL (headers carry the signed overrides)."""
        with self._lock:
            checked = self._check(url, "get", {})
            if isinstance(checked, SimulatedResponse):
                return checked
            obj = self._objects.get(checked.key)
            if obj is None:
                return SimulatedResponse(404, body=b"NoSuchKey")
            headers = {
                "content-type": checked.response_content_type
                or obj.content_type
                or "application/octet-stream",
                "content-disposition": checked.disposition or "",
                "etag": f'"{obj.etag}"',
            }
            return SimulatedResponse(200, headers, obj.data)
