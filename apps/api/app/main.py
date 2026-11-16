"""FastAPI application factory.

Run: ``uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000``
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.core.config import Settings, get_settings


def _configure_logging(settings: Settings) -> None:
    logging.getLogger("ukos").setLevel(settings.UKOS_LOG_LEVEL.upper())


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings)

    app = FastAPI(
        title="Universal Knowledge OS API",
        version="0.1.0",
        openapi_url="/openapi.json",
    )
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
