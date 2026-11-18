"""Stable error codes of the documents API (contract m02-contracts.md §5).

Responses are ``{"detail": "<code>"}``; build them with the shared ``app.api.deps.api_error`` (the
helpers below only fix the status code for each code). The M01 codes (``not_found``, ``forbidden``,
``invalid_request``, ``invalid_cursor``, ``conflict``) are re-exported for one import place.
"""

from __future__ import annotations

from typing import Final

from fastapi import HTTPException, status

from app.api.deps import (
    CONFLICT,
    FORBIDDEN,
    INVALID_CURSOR,
    INVALID_REQUEST,
    NOT_FOUND,
    api_error,
)

# 409
QUOTA_EXCEEDED: Final = "quota_exceeded"
OBJECT_MISSING: Final = "object_missing"
SIZE_MISMATCH: Final = "size_mismatch"
CHECKSUM_MISMATCH: Final = "checksum_mismatch"
# 410
UPLOAD_EXPIRED: Final = "upload_expired"
# 413
FILE_TOO_LARGE: Final = "file_too_large"
# 429
TOO_MANY_UPLOADS: Final = "too_many_uploads"
# 503
STORAGE_UNAVAILABLE: Final = "storage_unavailable"

# ``upload_sessions.failure_code`` values (state ``failed``).
FAILURE_SIZE_MISMATCH: Final = SIZE_MISMATCH
FAILURE_CHECKSUM_MISMATCH: Final = CHECKSUM_MISMATCH

__all__ = [
    "CHECKSUM_MISMATCH",
    "CONFLICT",
    "FAILURE_CHECKSUM_MISMATCH",
    "FAILURE_SIZE_MISMATCH",
    "FILE_TOO_LARGE",
    "FORBIDDEN",
    "INVALID_CURSOR",
    "INVALID_REQUEST",
    "NOT_FOUND",
    "OBJECT_MISSING",
    "QUOTA_EXCEEDED",
    "SIZE_MISMATCH",
    "STORAGE_UNAVAILABLE",
    "TOO_MANY_UPLOADS",
    "UPLOAD_EXPIRED",
    "checksum_mismatch",
    "file_too_large",
    "object_missing",
    "quota_exceeded",
    "size_mismatch",
    "storage_unavailable",
    "too_many_uploads",
    "upload_expired",
]


def file_too_large() -> HTTPException:
    # Literal 413: Starlette renamed the HTTP_413_* constant between versions.
    return api_error(413, FILE_TOO_LARGE)


def too_many_uploads() -> HTTPException:
    return api_error(status.HTTP_429_TOO_MANY_REQUESTS, TOO_MANY_UPLOADS)


def quota_exceeded() -> HTTPException:
    return api_error(status.HTTP_409_CONFLICT, QUOTA_EXCEEDED)


def object_missing() -> HTTPException:
    return api_error(status.HTTP_409_CONFLICT, OBJECT_MISSING)


def size_mismatch() -> HTTPException:
    return api_error(status.HTTP_409_CONFLICT, SIZE_MISMATCH)


def checksum_mismatch() -> HTTPException:
    return api_error(status.HTTP_409_CONFLICT, CHECKSUM_MISMATCH)


def upload_expired() -> HTTPException:
    return api_error(status.HTTP_410_GONE, UPLOAD_EXPIRED)


def storage_unavailable() -> HTTPException:
    return api_error(status.HTTP_503_SERVICE_UNAVAILABLE, STORAGE_UNAVAILABLE)
