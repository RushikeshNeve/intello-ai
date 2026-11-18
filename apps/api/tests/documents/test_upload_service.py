"""Pure helpers of the upload endpoints (M02.01.03, M02.01.04): no database, no object store."""

from __future__ import annotations

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.modules.documents import errors, upload, upload_service
from app.modules.storage.ports import ChecksumMismatch, StorageUnavailable

VALID = {
    "filename": "a.txt",
    "content_type": "text/plain",
    "size_bytes": 3,
    "sha256": "a" * 64,
}


def test_presign_ttl_defaults_and_follows_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("S3_PRESIGN_TTL_SECONDS", raising=False)
    assert upload_service.get_presign_ttl_seconds() == 300
    monkeypatch.setenv("S3_PRESIGN_TTL_SECONDS", "90")
    assert upload_service.get_presign_ttl_seconds() == 90


@pytest.mark.parametrize("value", ["0", "-5", "999999999", "abc"])
def test_presign_ttl_rejects_nonsense(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("S3_PRESIGN_TTL_SECONDS", value)
    with pytest.raises(ValidationError):
        upload_service.get_presign_ttl_seconds()


def test_presign_ttl_does_not_need_store_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("S3_SECRET_ACCESS_KEY", raising=False)
    assert upload_service.get_presign_ttl_seconds() >= 1


def test_every_storage_error_becomes_503_without_detail() -> None:
    for exc in (StorageUnavailable("secret-host:9000 refused"), ChecksumMismatch("x")):
        http = upload_service.storage_error_to_http(exc)
        assert (http.status_code, http.detail) == (503, errors.STORAGE_UNAVAILABLE)


def test_failure_codes_map_back_to_their_409() -> None:
    size = upload_service.failure_http(errors.SIZE_MISMATCH)
    checksum = upload_service.failure_http(errors.CHECKSUM_MISMATCH)
    assert (size.status_code, size.detail) == (409, "size_mismatch")
    assert (checksum.status_code, checksum.detail) == (409, "checksum_mismatch")
    assert upload_service.failure_http("anything else").detail == "checksum_mismatch"


def test_body_validation_after_authorization_uses_the_standard_422_shape() -> None:
    assert upload._parse_body(VALID).filename == "a.txt"
    with pytest.raises(RequestValidationError) as exc:
        upload._parse_body({**VALID, "sha256": "short"})
    locs = [tuple(err["loc"]) for err in exc.value.errors()]
    assert ("body", "sha256") in locs


def test_body_validation_never_leaks_the_input() -> None:
    with pytest.raises(RequestValidationError) as exc:
        upload._parse_body({**VALID, "size_bytes": 0, "filename": "///"})
    assert {err["type"] for err in exc.value.errors()} >= {"greater_than_equal", "value_error"}
