"""Object-storage port (M02.01.01, contract docs/architecture/m02-contracts.md section 2).

The port is the only thing the rest of the API knows about the object store. It is deliberately
limited to what both the local RustFS store and Cloudflare R2 support: put/get/head/delete,
SigV4 presigned PUT/GET/UploadPart, ``x-amz-checksum-sha256`` and multipart. Implementations:
``app.infrastructure.object_storage.s3.S3ObjectStore`` (boto3) and
``app.infrastructure.object_storage.memory.InMemoryObjectStore`` (tests).

Conventions shared by every implementation:

* A *key* is an opaque string such as ``org/<org>/doc/<doc>/<version>/original`` (see ``keys.py``).
  One store instance is bound to one bucket.
* ``sha256_b64`` is the standard base64 of the raw 32-byte SHA-256 digest (the format of the
  ``x-amz-checksum-sha256`` header), not hex.
* Exceptions never carry credentials, presigned URLs or request signatures; they carry a short
  operation name and a provider error code only.
* Backend connectivity problems (timeouts, connection errors, 5xx, rejected credentials) always
  surface as :class:`StorageUnavailable`, so callers can answer 503 ``storage_unavailable``.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

# --------------------------------------------------------------------------------- errors


class StorageError(Exception):
    """Base class of every error raised by an :class:`ObjectStore`."""


class ObjectNotFound(StorageError):
    """The key (or, for multipart calls, the upload) does not exist."""


class MultipartUploadNotFound(ObjectNotFound):
    """The multipart upload id is unknown (never created, completed or aborted)."""


class ObjectAlreadyExists(StorageError):
    """A conditional create (``If-None-Match: *``) found the key already present (HTTP 412).

    Keys of originals are never reused (the version id is part of the key), so this signals a bug
    or a replayed request, never success.
    """


class ChecksumMismatch(StorageError):
    """The store rejected the body because it does not match the supplied SHA-256."""


class InvalidMultipart(StorageError):
    """Complete refused: unknown part, wrong ETag, bad order or a non-last part under 5 MiB."""


class StorageUnavailable(StorageError):
    """The store is unreachable or failed (timeouts, connection errors, 5xx, bad credentials)."""


# ------------------------------------------------------------------------------ data types


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """Metadata of a stored object.

    ``checksum_sha256_b64`` is the stored whole-object SHA-256 for objects written with a checksum
    in one request (PUT); it is ``None`` when the store did not persist one, and is not the
    whole-object digest for completed multipart uploads.
    """

    key: str
    size: int
    content_type: str | None
    checksum_sha256_b64: str | None
    etag: str | None
    last_modified: datetime | None


@dataclass(frozen=True, slots=True)
class PresignedRequest:
    """A request the *browser* must send as-is.

    ``headers`` is exactly the set of headers that are part of the signature and must be sent with
    the stated values (the browser adds ``Host``/``Content-Length`` itself). ``expires_at`` is
    timezone-aware (UTC).
    """

    method: str
    url: str
    headers: dict[str, str]
    expires_at: datetime

    def __repr__(self) -> str:  # the URL carries the signature: keep it out of logs
        return f"PresignedRequest(method={self.method!r}, expires_at={self.expires_at.isoformat()})"


@dataclass(frozen=True, slots=True)
class MultipartUpload:
    key: str
    upload_id: str


@dataclass(frozen=True, slots=True)
class PartInfo:
    """A part already stored for an open multipart upload (from ``list_parts``)."""

    part_number: int
    etag: str
    size: int


@dataclass(frozen=True, slots=True)
class CompletedPart:
    """A part to include in ``complete_multipart`` (ETag as returned by the part upload)."""

    part_number: int
    etag: str


@dataclass(frozen=True, slots=True)
class MultipartLimits:
    """Provider-independent S3 multipart limits (also R2's)."""

    min_part_bytes: int = 5 * 1024 * 1024
    max_parts: int = 10_000


MULTIPART_LIMITS = MultipartLimits()


# ------------------------------------------------------------------------------- the port


@runtime_checkable
class ObjectStore(Protocol):
    """S3-compatible object store bound to one bucket."""

    def put_object(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        sha256_b64: str,
        if_none_match: bool = True,
    ) -> ObjectInfo:
        """Store ``data`` under ``key`` (server-side upload, e.g. generated artifacts).

        The store verifies ``sha256_b64`` and rejects a different body: :class:`ChecksumMismatch`,
        nothing stored. With ``if_none_match`` (default) the write is a conditional create and an
        existing key raises :class:`ObjectAlreadyExists` (originals are immutable).
        """
        ...

    def head_object(self, key: str) -> ObjectInfo | None:
        """Metadata of ``key`` including the stored SHA-256, or ``None`` when it does not exist."""
        ...

    def get_object(self, key: str) -> Iterator[bytes]:
        """Stream the object's bytes. Raises :class:`ObjectNotFound` immediately (before iteration).

        The iterator releases its connection when exhausted; call ``close()`` if you stop early.
        """
        ...

    def delete_object(self, key: str) -> None:
        """Delete ``key``. **Idempotent**: deleting a missing key succeeds silently."""
        ...

    def presign_put(
        self, key: str, *, content_type: str, sha256_b64: str, expires_seconds: int
    ) -> PresignedRequest:
        """Presigned PUT bound to ``content_type`` and ``sha256_b64`` (both signed headers).

        ``PresignedRequest.headers`` holds ``Content-Type`` and ``x-amz-checksum-sha256``; the store
        rejects a request with other values (403) or a body not matching the checksum (400). Pure
        computation: no network call, never raises :class:`StorageUnavailable`.
        """
        ...

    def presign_get(
        self,
        key: str,
        *,
        expires_seconds: int,
        filename: str,
        content_type: str | None,
        inline: bool,
    ) -> str:
        """Presigned GET URL with a signed ``response-content-disposition`` (and content-type).

        The filename is sanitized and sent as RFC 5987 ``filename*`` plus an ASCII ``filename``
        fallback. ``inline=False`` (default for callers) gives ``attachment``; ``inline=True`` is
        the caller's explicit decision (see the allow-list in contract section 6). A non-``None``
        ``content_type`` overrides the response ``Content-Type``. Pure computation.
        """
        ...

    def create_multipart(self, key: str, *, content_type: str) -> MultipartUpload:
        """Start a multipart upload for ``key``."""
        ...

    def presign_upload_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        *,
        expires_seconds: int,
        sha256_b64: str | None = None,
    ) -> PresignedRequest:
        """Presigned PUT for one part (1..10000); ``sha256_b64`` also binds the part checksum."""
        ...

    def complete_multipart(
        self, key: str, upload_id: str, parts: Sequence[CompletedPart]
    ) -> ObjectInfo:
        """Assemble the parts (ascending ``part_number``, ETags as returned by the part PUTs).

        Raises :class:`MultipartUploadNotFound` for an unknown upload and :class:`InvalidMultipart`
        for a bad part list.
        """
        ...

    def abort_multipart(self, key: str, upload_id: str) -> None:
        """Abort and discard the parts. **Idempotent**: an unknown upload is not an error."""
        ...

    def list_parts(self, key: str, upload_id: str) -> list[PartInfo]:
        """All parts stored so far, ascending; :class:`MultipartUploadNotFound` if unknown."""
        ...
