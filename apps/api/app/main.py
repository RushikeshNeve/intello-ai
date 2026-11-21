"""FastAPI application factory.

Run: ``uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000``
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from app.api import health, me
from app.api.deps import validation_error_handler
from app.core import tracing
from app.core.config import Settings, get_settings
from app.core.db import dispose_engines, get_engine
from app.core.security import (
    NotConfiguredError,
    UserProvisioner,
    get_token_verifier,
    get_user_provisioner,
)
from app.modules.ai.chat_router import router as chat_router
from app.modules.ai.policy_router import router as ai_policy_router
from app.modules.connectors.router import router as connectors_router
from app.modules.connectors.router import webhook_router as connectors_webhook_router
from app.modules.connectors.url_capture import router as url_capture_router
from app.modules.documents import router as documents_router
from app.modules.graph.api import router as graph_router
from app.modules.graph.router_review import router as graph_review_router
from app.modules.identity.oidc import OidcTokenVerifier
from app.modules.identity.provisioning import DbUserProvisioner
from app.modules.ingestion.capabilities_router import router as capabilities_router
from app.modules.ingestion.jobs_list import router as ingestion_jobs_router
from app.modules.ingestion.router import router as ingestion_router
from app.modules.memory.router import router as memory_router
from app.modules.organizations.router import router as organizations_router
from app.modules.research.router import router as research_router
from app.modules.retrieval.router import router as retrieval_router
from app.modules.usage.router import router as usage_router
from app.modules.workspaces.overview import router as overview_router
from app.modules.workspaces.router import router as workspaces_router

logger = logging.getLogger("ukos.app")


def _configure_logging(settings: Settings) -> None:
    logging.getLogger("ukos").setLevel(settings.UKOS_LOG_LEVEL.upper())
    # M08.02.04: JSON logs with correlation ids; optional OTLP tracing and metrics listener.
    tracing.setup_process(settings, "api")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.modules.ai.warmup import start_warmup_background

    start_warmup_background()  # M11.05, best effort
    yield
    verifier = getattr(app.state, "token_verifier", None)
    if isinstance(verifier, OidcTokenVerifier):
        verifier.close()
    dispose_engines()


def _db_user_provisioner(engine: Annotated[Engine, Depends(get_engine)]) -> UserProvisioner:
    # Follows ``get_engine`` (and its overrides), i.e. the app-role engine for these settings.
    return DbUserProvisioner(engine)


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
    # Tenancy routes (M01.02.02/04): 422 bodies keep the {"detail": "<code>"} error shape.
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.dependency_overrides[get_settings] = lambda: settings

    # Identity (M01.01.01 / M01.01.03): one verifier per app (shared JWKS cache; no network until
    # the first token) and a provisioner on the app-role engine.
    token_verifier = OidcTokenVerifier(settings)
    app.state.token_verifier = token_verifier
    app.dependency_overrides[get_token_verifier] = lambda: token_verifier
    app.dependency_overrides[get_user_provisioner] = _db_user_provisioner

    if settings.METRICS_PORT:
        tracing.metrics()
    app.add_middleware(tracing.TraceMiddleware)

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
    app.include_router(me.router)
    app.include_router(organizations_router)
    app.include_router(workspaces_router)
    app.include_router(documents_router)
    app.include_router(ingestion_router)
    app.include_router(ingestion_jobs_router)
    app.include_router(overview_router)
    app.include_router(capabilities_router)
    app.include_router(url_capture_router)
    app.include_router(retrieval_router)
    app.include_router(graph_router)
    app.include_router(ai_policy_router)
    app.include_router(chat_router)
    app.include_router(connectors_router)
    app.include_router(connectors_webhook_router)
    app.include_router(graph_review_router)
    app.include_router(memory_router)
    app.include_router(research_router)
    app.include_router(usage_router)
    return app
