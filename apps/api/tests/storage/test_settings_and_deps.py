"""Unit tests: ObjectStoreSettings validation and the get_object_store dependency."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.infrastructure.object_storage.s3 import S3ObjectStore
from app.infrastructure.object_storage.settings import ObjectStoreSettings
from app.modules.storage.deps import get_object_store, reset_object_store_cache
from app.modules.storage.ports import ObjectStore, StorageUnavailable

CREDS = {"S3_ACCESS_KEY_ID": "AKIATESTKEY", "S3_SECRET_ACCESS_KEY": "super-secret-value"}


def make(**overrides: object) -> ObjectStoreSettings:
    return ObjectStoreSettings(
        S3_ACCESS_KEY_ID=SecretStr(CREDS["S3_ACCESS_KEY_ID"]),
        S3_SECRET_ACCESS_KEY=SecretStr(CREDS["S3_SECRET_ACCESS_KEY"]),
        **overrides,  # type: ignore[arg-type]
    )


def test_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {
        **CREDS,
        "S3_ENDPOINT_URL": "http://objectstore:9000/",
        "S3_PUBLIC_ENDPOINT_URL": "https://files.example.test",
        "S3_REGION": "auto",
        "S3_FORCE_PATH_STYLE": "false",
        "S3_PRESIGN_TTL_SECONDS": "120",
        "UNRELATED_VARIABLE": "ignored",
    }.items():
        monkeypatch.setenv(k, v)
    s = ObjectStoreSettings()
    assert s.S3_ENDPOINT_URL == "http://objectstore:9000"  # trailing slash dropped
    assert s.S3_PUBLIC_ENDPOINT_URL == "https://files.example.test"
    assert (s.S3_REGION, s.S3_FORCE_PATH_STYLE, s.S3_PRESIGN_TTL_SECONDS) == ("auto", False, 120)
    assert s.S3_BUCKET_ORIGINALS == "ukos-originals"


def test_credentials_required_and_never_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("S3_SECRET_ACCESS_KEY", raising=False)
    with pytest.raises(ValidationError):
        ObjectStoreSettings()
    s = make()
    assert "super-secret-value" not in repr(s) + str(s)
    assert "AKIATESTKEY" not in repr(s) + str(s)
    store = S3ObjectStore(s)
    assert "super-secret-value" not in repr(store)
    assert "AKIATESTKEY" not in repr(store)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("S3_ENDPOINT_URL", "ftp://host"),
        ("S3_ENDPOINT_URL", "objectstore:9000"),
        ("S3_ENDPOINT_URL", "http://user:pw@host:9000"),
        ("S3_PUBLIC_ENDPOINT_URL", "http://host/bucket"),
        ("S3_PUBLIC_ENDPOINT_URL", "http://host?x=1"),
        ("S3_BUCKET_ORIGINALS", "UPPER"),
        ("S3_BUCKET_ORIGINALS", "ab"),
        ("S3_BUCKET_ARTIFACTS", "a..b-bucket"),
        ("S3_PRESIGN_TTL_SECONDS", 0),
        ("S3_PRESIGN_TTL_SECONDS", 7 * 24 * 3600 + 1),
        ("S3_MAX_ATTEMPTS", 0),
        ("S3_READ_TIMEOUT_SECONDS", 0),
    ],
)
def test_invalid_values_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError) as exc:
        make(**{field: value})
    assert "super-secret-value" not in str(exc.value)


def test_error_text_does_not_echo_credentials_in_endpoint() -> None:
    with pytest.raises(ValidationError) as exc:
        make(S3_ENDPOINT_URL="http://user:topsecretpw@host:9000")
    assert "topsecretpw" not in str(exc.value)


# ----------------------------------------------------------------------------- dependency


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    reset_object_store_cache()
    yield
    reset_object_store_cache()


def test_get_object_store_builds_once(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)
    first = get_object_store()
    assert isinstance(first, S3ObjectStore)
    assert isinstance(first, ObjectStore)  # runtime-checkable protocol
    assert get_object_store() is first


def test_get_object_store_unconfigured_is_storage_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("S3_SECRET_ACCESS_KEY", raising=False)
    with pytest.raises(StorageUnavailable, match="S3_ACCESS_KEY_ID"):
        get_object_store()


def test_dependency_override_in_an_app() -> None:
    app = FastAPI()
    store = InMemoryObjectStore()

    @app.get("/probe")
    def probe(s: ObjectStore = Depends(get_object_store)) -> dict[str, bool]:  # noqa: B008
        return {"memory": isinstance(s, InMemoryObjectStore)}

    app.dependency_overrides[get_object_store] = lambda: store
    assert TestClient(app).get("/probe").json() == {"memory": True}


def test_both_adapters_satisfy_the_port() -> None:
    assert isinstance(InMemoryObjectStore(), ObjectStore)
