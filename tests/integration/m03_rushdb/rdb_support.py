"""Helpers for the RushDB integration tests (not in conftest.py so tests can import them)."""

from __future__ import annotations

import json
import statistics
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import Engine, text

from app.infrastructure.rushdb.settings import RushDbSettings


class RushDbStack:
    """The running RushDB container: its URL (as seen by the tests), token and compose project."""

    def __init__(self, project: Any, url: str, token: str) -> None:
        self.project = project
        self.url = url
        self.token = token

    def settings(self, **overrides: Any) -> RushDbSettings:
        values: dict[str, Any] = {"RUSHDB_URL": self.url, "RUSHDB_TOKEN": self.token}
        values.update(overrides)
        return RushDbSettings(**values)


class RecordingTransport:
    """Wraps a real HTTP transport and records every request the adapter sends."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []

    def wrap(self) -> httpx.BaseTransport:
        outer = self
        inner = httpx.HTTPTransport()

        class _T(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                outer.requests.append(request)
                outer.bodies.append(json.loads(request.content))
                return inner.handle_request(request)

            def close(self) -> None:
                inner.close()

        return _T()

    @property
    def operations(self) -> list[dict[str, Any]]:
        return [b["operation"] for b in self.bodies]

    def clear(self) -> None:
        self.requests.clear()
        self.bodies.clear()


def raw_execute(
    url: str, token: str, tenant: str, operation: dict[str, Any], *, header: str | None = None
) -> dict[str, Any]:
    """One raw Protocol v1 call (tests peek at what is really stored); returns the response body."""
    response = httpx.post(
        f"{url}/protocol/v1/execute",
        json={"protocolVersion": 1, "context": {"scope": tenant}, "operation": operation},
        headers={"Authorization": f"Bearer {token}", "X-RushDB-Tenant": header or tenant},
        timeout=60.0,
    )
    body: dict[str, Any] = response.json()
    return body


def purge_tenant(url: str, token: str, tenant: str) -> None:
    raw_execute(url, token, tenant, {"kind": "purgeTenant"})


def soft_delete_org(admin_engine: Engine, org_id: uuid.UUID) -> None:
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE organizations SET deleted_at = now() WHERE id = :i"), {"i": org_id}
        )


def time_calls(fn: Callable[[], Any], repeat: int) -> tuple[float, float]:
    """Run ``fn`` ``repeat`` times; return (p50, p95) wall-clock milliseconds."""
    samples: list[float] = []
    for _ in range(repeat):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    return statistics.median(samples), p95
