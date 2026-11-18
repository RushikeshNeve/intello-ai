"""S3-compatible ``ObjectStore`` adapter on boto3 (M02.01.02): RustFS locally, Cloudflare R2 later.

* SigV4, path-style (``S3_FORCE_PATH_STYLE``), ``request_checksum_calculation="when_required"`` so
  botocore adds no default CRC32 trailers; SHA-256 is sent explicitly where the contract says so.
* Two clients: server-side calls use ``S3_ENDPOINT_URL``; presigned URLs are signed by a second
  client configured with ``S3_PUBLIC_ENDPOINT_URL`` (the host is part of the signature).
* Errors are mapped to the typed port errors with ``from None`` and a fixed message, so neither
  credentials nor URLs reach exception text; logs carry the operation and provider error code only.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.infrastructure.object_storage.settings import ObjectStoreSettings
from app.modules.storage.naming import (
    content_disposition,
    is_valid_content_type,
    validate_content_type,
    validate_expiry,
    validate_part_number,
    validate_sha256_b64,
)
from app.modules.storage.ports import (
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
    StorageUnavailable,
)

logger = logging.getLogger("ukos.storage")

_CHUNK_BYTES = 1024 * 1024
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NotFound", "404"})
_NO_UPLOAD_CODES = frozenset({"NoSuchUpload"})
_CHECKSUM_CODES = frozenset(
    {
        "BadDigest",
        "InvalidDigest",
        "XAmzContentSHA256Mismatch",
        "XAmzContentChecksumMismatch",
        "ChecksumMismatch",
    }
)
_INVALID_PART_CODES = frozenset(
    {"InvalidPart", "InvalidPartOrder", "EntityTooSmall", "MalformedXML", "InvalidRequest"}
)
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def _meta(exc: ClientError) -> tuple[str, int]:
    err = exc.response.get("Error", {})
    meta = exc.response.get("ResponseMetadata", {})
    return str(err.get("Code", "")), int(meta.get("HTTPStatusCode", 0))


def _is_not_found(exc: ClientError) -> bool:
    code, status = _meta(exc)
    return code in _NOT_FOUND_CODES or (status == 404 and code != "NoSuchBucket")


def _build_client(settings: ObjectStoreSettings, endpoint: str, *, presign: bool) -> Any:
    config = Config(
        signature_version="s3v4",
        s3={"addressing_style": "path" if settings.S3_FORCE_PATH_STYLE else "virtual"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        retries={
            "total_max_attempts": 1 if presign else settings.S3_MAX_ATTEMPTS,
            "mode": "standard",
        },
        connect_timeout=settings.S3_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.S3_READ_TIMEOUT_SECONDS,
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=settings.S3_REGION,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID.get_secret_value(),
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY.get_secret_value(),
        config=config,
    )


def _etag(value: object) -> str | None:
    return str(value).strip('"') if value else None


def _info(key: str, resp: dict[str, Any]) -> ObjectInfo:
    return ObjectInfo(
        key=key,
        size=int(resp["ContentLength"]),
        content_type=resp.get("ContentType"),
        checksum_sha256_b64=resp.get("ChecksumSHA256"),
        etag=_etag(resp.get("ETag")),
        last_modified=resp.get("LastModified"),
    )


class S3ObjectStore:
    """``ObjectStore`` implementation for one bucket (default: ``S3_BUCKET_ORIGINALS``)."""

    def __init__(
        self,
        settings: ObjectStoreSettings,
        *,
        bucket: str | None = None,
        client: Any = None,
        presign_client: Any = None,
    ) -> None:
        self._bucket = bucket or settings.S3_BUCKET_ORIGINALS
        self._client = client or _build_client(settings, settings.S3_ENDPOINT_URL, presign=False)
        self._presign = presign_client or _build_client(
            settings, settings.S3_PUBLIC_ENDPOINT_URL, presign=True
        )

    def __repr__(self) -> str:
        return f"S3ObjectStore(bucket={self._bucket!r})"

    # ------------------------------------------------------------------ error mapping

    def _fail(self, op: str, exc: Exception) -> NoReturn:
        """Translate a botocore error into a port error. Always raises."""
        if isinstance(exc, ClientError):
            code, status = _meta(exc)
            logger.warning("object store %s failed: code=%s status=%s", op, code, status)
            if code in _NO_UPLOAD_CODES:
                raise MultipartUploadNotFound(f"{op}: no such multipart upload") from None
            if _is_not_found(exc):
                raise ObjectNotFound(f"{op}: not found") from None
            if code == "PreconditionFailed" or status == 412:
                raise ObjectAlreadyExists(f"{op}: object already exists") from None
            if code in _CHECKSUM_CODES:
                raise ChecksumMismatch(f"{op}: checksum mismatch ({code})") from None
            if code in _INVALID_PART_CODES and op == "complete_multipart":
                raise InvalidMultipart(f"{op}: invalid part list ({code})") from None
            # 5xx, throttling, conditional-request conflicts, rejected credentials, missing bucket.
            raise StorageUnavailable(f"{op}: object store error ({code or status})") from None
        logger.warning("object store %s failed: %s", op, type(exc).__name__)
        raise StorageUnavailable(f"{op}: object store unreachable ({type(exc).__name__})") from None

    # ------------------------------------------------------------------ objects

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
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": data,
            "ContentType": content_type,
            "ChecksumSHA256": sha256_b64,
        }
        if if_none_match:
            params["IfNoneMatch"] = "*"
        try:
            resp = self._client.put_object(**params)
        except (ClientError, BotoCoreError) as exc:
            self._fail("put_object", exc)
        return ObjectInfo(
            key=key,
            size=len(data),
            content_type=content_type,
            checksum_sha256_b64=resp.get("ChecksumSHA256") or sha256_b64,
            etag=_etag(resp.get("ETag")),
            last_modified=None,
        )

    def head_object(self, key: str) -> ObjectInfo | None:
        try:
            resp = self._client.head_object(Bucket=self._bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as exc:
            if _is_not_found(exc):
                return None
            self._fail("head_object", exc)
        except BotoCoreError as exc:
            self._fail("head_object", exc)
        return _info(key, resp)

    def get_object(self, key: str) -> Iterator[bytes]:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
        except (ClientError, BotoCoreError) as exc:
            self._fail("get_object", exc)
        return self._stream(resp["Body"])

    def _stream(self, body: Any) -> Iterator[bytes]:
        try:
            yield from body.iter_chunks(_CHUNK_BYTES)
        except (BotoCoreError, OSError) as exc:  # truncated or timed-out transfer
            logger.warning("object store get_object stream failed: %s", type(exc).__name__)
            raise StorageUnavailable("get_object: transfer interrupted") from None
        finally:
            body.close()

    def delete_object(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                return
            self._fail("delete_object", exc)
        except BotoCoreError as exc:
            self._fail("delete_object", exc)

    # ------------------------------------------------------------------ presigned URLs

    def _presigned(
        self, operation: str, params: dict[str, Any], headers: dict[str, str], expires: int
    ) -> PresignedRequest:
        url: str = self._presign.generate_presigned_url(
            operation, Params=params, ExpiresIn=expires, HttpMethod="PUT"
        )
        return PresignedRequest(
            method="PUT",
            url=url,
            headers=headers,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires),
        )

    def presign_put(
        self, key: str, *, content_type: str, sha256_b64: str, expires_seconds: int
    ) -> PresignedRequest:
        validate_sha256_b64(sha256_b64)
        content_type = validate_content_type(content_type)
        expires = validate_expiry(expires_seconds)
        params = {
            "Bucket": self._bucket,
            "Key": key,
            "ContentType": content_type,
            "ChecksumSHA256": sha256_b64,
        }
        headers = {"Content-Type": content_type, "x-amz-checksum-sha256": sha256_b64}
        return self._presigned("put_object", params, headers, expires)

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
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "ResponseContentDisposition": content_disposition(filename, inline=inline),
        }
        if content_type is not None:
            params["ResponseContentType"] = (
                content_type if is_valid_content_type(content_type) else _DEFAULT_CONTENT_TYPE
            )
        url: str = self._presign.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires
        )
        return url

    # ------------------------------------------------------------------ multipart

    def create_multipart(self, key: str, *, content_type: str) -> MultipartUpload:
        content_type = validate_content_type(content_type)
        try:
            resp = self._client.create_multipart_upload(
                Bucket=self._bucket, Key=key, ContentType=content_type
            )
        except (ClientError, BotoCoreError) as exc:
            self._fail("create_multipart", exc)
        return MultipartUpload(key=key, upload_id=str(resp["UploadId"]))

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
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        }
        headers: dict[str, str] = {}
        if sha256_b64 is not None:
            params["ChecksumSHA256"] = validate_sha256_b64(sha256_b64)
            headers["x-amz-checksum-sha256"] = sha256_b64
        return self._presigned("upload_part", params, headers, expires)

    def complete_multipart(
        self, key: str, upload_id: str, parts: Sequence[CompletedPart]
    ) -> ObjectInfo:
        numbers = [p.part_number for p in parts]
        if not parts or numbers != sorted(set(numbers)):
            raise InvalidMultipart("complete_multipart: parts must be unique and ascending")
        listing = [{"PartNumber": p.part_number, "ETag": p.etag} for p in parts]
        try:
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": listing},
            )
        except (ClientError, BotoCoreError) as exc:
            self._fail("complete_multipart", exc)
        info = self.head_object(key)
        if info is None:  # completed, then gone: a concurrent delete
            raise ObjectNotFound("complete_multipart: object vanished")
        return info

    def abort_multipart(self, key: str, upload_id: str) -> None:
        try:
            self._client.abort_multipart_upload(Bucket=self._bucket, Key=key, UploadId=upload_id)
        except ClientError as exc:
            code, status = _meta(exc)
            if code in _NO_UPLOAD_CODES or status == 404:
                return
            self._fail("abort_multipart", exc)
        except BotoCoreError as exc:
            self._fail("abort_multipart", exc)

    def list_parts(self, key: str, upload_id: str) -> list[PartInfo]:
        parts: list[PartInfo] = []
        marker = 0
        try:
            while True:
                resp = self._client.list_parts(
                    Bucket=self._bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumberMarker=marker,
                    MaxParts=1000,
                )
                for p in resp.get("Parts", []):
                    parts.append(
                        PartInfo(
                            int(p["PartNumber"]), _etag(p.get("ETag")) or "", int(p.get("Size", 0))
                        )
                    )
                if not resp.get("IsTruncated"):
                    return parts
                marker = int(resp["NextPartNumberMarker"])
        except (ClientError, BotoCoreError) as exc:
            self._fail("list_parts", exc)
