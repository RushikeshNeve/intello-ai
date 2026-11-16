from __future__ import annotations

from collections.abc import Mapping

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.health import DependencyCheck, get_dependency_checks
from app.core.config import Settings
from app.main import create_app

SECRET_DB_PASSWORD = "s3cr3t-db-password-do-not-leak"
SECRET_REDIS_PASSWORD = "s3cr3t-redis-password-do-not-leak"


def _unreachable_settings() -> Settings:
    # Port 1 on loopback refuses connections immediately; credentials must never be echoed.
    return Settings(
        DATABASE_URL=SecretStr(
            f"postgresql+psycopg://ukos_app:{SECRET_DB_PASSWORD}@127.0.0.1:1/ukos"
        ),
        REDIS_URL=SecretStr(f"redis://:{SECRET_REDIS_PASSWORD}@127.0.0.1:1/0"),
        API_CORS_ORIGINS="http://127.0.0.1:3300,http://localhost:3300",
        HEALTH_CHECK_TIMEOUT_SECONDS=1.0,
    )


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(_unreachable_settings()))


def _ok() -> None:
    return None


def _boom() -> None:
    raise ConnectionError(f"could not connect: password={SECRET_DB_PASSWORD}")


def test_live_ok_without_dependencies(client: TestClient) -> None:
    res = client.get("/health/live")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_ready_503_when_dependencies_unreachable(client: TestClient) -> None:
    res = client.get("/health/ready")
    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "unavailable"
    assert set(body["checks"]) == {"postgres", "redis"}
    assert body["checks"]["postgres"]["status"] in {"error", "timeout"}
    assert body["checks"]["redis"]["status"] in {"error", "timeout"}


def test_ready_never_leaks_secrets(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    res = client.get("/health/ready")
    for secret in (SECRET_DB_PASSWORD, SECRET_REDIS_PASSWORD, "127.0.0.1:1", "ukos_app"):
        assert secret not in res.text
        assert secret not in caplog.text


def _client_with_checks(checks: Mapping[str, DependencyCheck]) -> TestClient:
    app = create_app(_unreachable_settings())
    app.dependency_overrides[get_dependency_checks] = lambda: checks
    return TestClient(app)


def test_ready_200_when_all_checks_pass() -> None:
    res = _client_with_checks({"postgres": _ok, "redis": _ok}).get("/health/ready")
    assert res.status_code == 200
    assert res.json() == {
        "status": "ok",
        "checks": {"postgres": {"status": "ok"}, "redis": {"status": "ok"}},
    }


def test_ready_503_when_one_check_fails_without_leaking_error() -> None:
    res = _client_with_checks({"postgres": _boom, "redis": _ok}).get("/health/ready")
    assert res.status_code == 503
    assert res.json()["checks"] == {"postgres": {"status": "error"}, "redis": {"status": "ok"}}
    assert SECRET_DB_PASSWORD not in res.text


def test_cors_allows_configured_origin(client: TestClient) -> None:
    res = client.options(
        "/health/live",
        headers={"Origin": "http://127.0.0.1:3300", "Access-Control-Request-Method": "GET"},
    )
    assert res.headers.get("access-control-allow-origin") == "http://127.0.0.1:3300"
    denied = client.get("/health/live", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in denied.headers


def test_openapi_published(client: TestClient) -> None:
    res = client.get("/openapi.json")
    assert res.status_code == 200
    paths = res.json()["paths"]
    assert "/health/live" in paths
    assert "/health/ready" in paths


def test_settings_repr_hides_secrets() -> None:
    settings = _unreachable_settings()
    assert SECRET_DB_PASSWORD not in repr(settings)
    assert settings.psycopg_dsn.startswith("postgresql://")
