"""Liveness and readiness endpoints.

* ``GET /health/live``  - process is up; never touches a dependency.
* ``GET /health/ready`` - PostgreSQL (``SELECT 1``) and Redis (``PING``) answer within a short
  timeout; 503 when any required dependency is down.

Responses carry only a per-dependency status word. Exception messages are never returned or
logged verbatim because driver errors can embed hosts, user names or connection strings.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Annotated, Literal

import psycopg
import redis
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from app.core.config import Settings, get_settings

logger = logging.getLogger("ukos.health")

router = APIRouter(prefix="/health", tags=["health"])

DependencyCheck = Callable[[], None]


class LiveResponse(BaseModel):
    status: Literal["ok"]


class DependencyStatus(BaseModel):
    status: Literal["ok", "error", "timeout"]


class ReadyResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, DependencyStatus]


def _check_postgres(settings: Settings) -> None:
    timeout = max(1, round(settings.HEALTH_CHECK_TIMEOUT_SECONDS))
    with (
        psycopg.connect(settings.psycopg_dsn, connect_timeout=timeout, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT 1")
        if cur.fetchone() != (1,):
            raise RuntimeError("unexpected SELECT 1 result")


def _check_redis(settings: Settings) -> None:
    timeout = settings.HEALTH_CHECK_TIMEOUT_SECONDS
    client = redis.Redis.from_url(
        settings.REDIS_URL.get_secret_value(),
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )
    try:
        if client.ping() is not True:
            raise RuntimeError("unexpected PING result")
    finally:
        client.close()


def get_dependency_checks(
    settings: Annotated[Settings, Depends(get_settings)],
) -> Mapping[str, DependencyCheck]:
    """Required dependencies for readiness. Override in tests via ``dependency_overrides``."""
    return {
        "postgres": lambda: _check_postgres(settings),
        "redis": lambda: _check_redis(settings),
    }


async def _run_check(name: str, check: DependencyCheck, limit_seconds: float) -> DependencyStatus:
    try:
        await asyncio.wait_for(asyncio.to_thread(check), timeout=limit_seconds)
    except TimeoutError:
        logger.warning("readiness check timed out", extra={"dependency": name})
        return DependencyStatus(status="timeout")
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        logger.warning(
            "readiness check failed",
            extra={"dependency": name, "error_type": type(exc).__name__},
        )
        return DependencyStatus(status="error")
    return DependencyStatus(status="ok")


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    return LiveResponse(status="ok")


@router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyResponse}},
)
async def ready(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    checks: Annotated[Mapping[str, DependencyCheck], Depends(get_dependency_checks)],
) -> ReadyResponse:
    # Allow a little slack over the driver-level timeouts before giving up on a check.
    timeout = settings.HEALTH_CHECK_TIMEOUT_SECONDS + 1.0
    names = list(checks)
    results = await asyncio.gather(*(_run_check(n, checks[n], timeout) for n in names))
    by_name = dict(zip(names, results, strict=True))
    healthy = all(r.status == "ok" for r in results)
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(status="ok" if healthy else "unavailable", checks=by_name)
