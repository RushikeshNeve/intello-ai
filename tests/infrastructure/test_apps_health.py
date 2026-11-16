"""M00.02.05 acceptance: web and API health endpoints respond.

Builds and boots data.yml + apps.yml under the `ukos-platform` project.

Run:
    uv run --no-project --with pytest --with "psycopg[binary]" --with redis \
        python -m pytest -q tests/infrastructure/test_apps_health.py
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
from compose_support import ComposeProject, compose_file, require_docker, wait_until

require_docker()


@pytest.fixture(scope="module")
def stack() -> Iterator[ComposeProject]:
    project = ComposeProject([compose_file("data.yml"), compose_file("apps.yml")])
    project.down(volumes=True)
    try:
        project.up(build=True, wait=True, wait_timeout=300)
        yield project
    finally:
        project.down(volumes=True)


def _get(url: str) -> tuple[int, dict[str, Any], str]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as res:  # noqa: S310 - fixed loopback URL
            body = res.read().decode()
            return res.status, json.loads(body), body
    except urllib.error.HTTPError as err:
        body = err.read().decode()
        return err.code, json.loads(body), body


def _api(stack: ComposeProject, path: str) -> str:
    return f"http://127.0.0.1:{stack.env['API_HOST_PORT']}{path}"


def _web(stack: ComposeProject, path: str) -> str:
    return f"http://127.0.0.1:{stack.env['WEB_HOST_PORT']}{path}"


def test_containers_healthy_and_loopback_only(stack: ComposeProject) -> None:
    for service in ("postgres", "redis", "api", "web"):
        assert stack.health(service) == "healthy", stack.logs(service)
    for service in ("api", "web"):
        info = stack.inspect(service)
        assert info["Config"]["User"] not in ("", "root", "0"), f"{service} runs as root"
        bindings = stack.published_bindings(service)
        assert bindings and all(ip == "127.0.0.1" for ip, _ in bindings), bindings


def test_api_live(stack: ComposeProject) -> None:
    status, body, _ = _get(_api(stack, "/health/live"))
    assert (status, body) == (200, {"status": "ok"})


def test_api_ready_with_dependencies(stack: ComposeProject) -> None:
    status, body, raw = _get(_api(stack, "/health/ready"))
    assert status == 200, raw
    assert body == {
        "status": "ok",
        "checks": {"postgres": {"status": "ok"}, "redis": {"status": "ok"}},
    }
    for secret in (stack.env["POSTGRES_APP_PASSWORD"], stack.env["REDIS_PASSWORD"]):
        assert secret not in raw


def test_api_openapi(stack: ComposeProject) -> None:
    status, body, _ = _get(_api(stack, "/openapi.json"))
    assert status == 200
    assert "/health/ready" in body["paths"]


def test_web_health(stack: ComposeProject) -> None:
    status, body, _ = _get(_web(stack, "/api/health"))
    assert (status, body) == (200, {"status": "ok"})


def test_ready_503_when_redis_down(stack: ComposeProject) -> None:
    stack.stop("redis")
    try:

        def ready_unavailable() -> tuple[int, dict[str, Any], str] | None:
            result = _get(_api(stack, "/health/ready"))
            return result if result[0] == 503 else None

        status, body, raw = wait_until(ready_unavailable, timeout=30, description="ready -> 503")
        assert status == 503
        assert body["status"] == "unavailable"
        assert body["checks"]["postgres"] == {"status": "ok"}
        assert body["checks"]["redis"]["status"] in {"error", "timeout"}
        assert stack.env["REDIS_PASSWORD"] not in raw
        # Liveness is independent of dependencies.
        assert _get(_api(stack, "/health/live"))[0] == 200
    finally:
        stack.start("redis")
        stack.wait_healthy(["redis"])
    wait_until(
        lambda: _get(_api(stack, "/health/ready"))[0] == 200,
        timeout=30,
        description="ready -> 200 after redis restart",
    )
