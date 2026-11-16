"""FastAPI application factory.

Run: ``uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000``
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import health
from app.core.config import Settings, get_settings
from app.core.db import dispose_engines
from app.core.security import NotConfiguredError

logger = logging.getLogger("ukos.app")


def _configure_logging(settings: Settings) -> None:
    logging.getLogger("ukos").setLevel(settings.UKOS_LOG_LEVEL.upper())


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    dispose_engines()


async def _not_configured_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.error("security component not configured", extra={"error": str(exc)})
    return JSONResponse(status_code=500, content={"detail": "internal_error"})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings)

    app = FastAPI(
        title="Universal Knowledge OS API",
        version="0.1.0",
        openapi_url="/openapi.json",
        lifespan=_lifespan,
    )
    app.add_exception_handler(NotConfiguredError, _not_configured_handler)
    app.dependency_overrides[get_settings] = lambda: settings

    origins = settings.cors_origins
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(health.router)
    return app
