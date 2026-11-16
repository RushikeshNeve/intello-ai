"""PostgreSQL access: engine, sessions and per-transaction tenant context (ADR-013, ADR-028).

The request path connects as the least-privilege app role (``DATABASE_URL`` -> ``ukos_app``,
NOBYPASSRLS). Every unit of work runs inside :func:`tenant_session`, which opens a transaction
and sets ``app.current_user_id`` / ``app.current_org_id`` with ``set_config(..., true)`` so the
values are transaction-local and can never leak to the next user of a pooled connection.
RLS policies read them with ``nullif(current_setting(..., true), '')::uuid`` and fail closed when
they are unset.

Usage in a route (the org id comes from path resolution, never from the client body)::

    @router.get("/v1/orgs/{org_id}")
    def get_org(
        org_id: UUID,
        principal: Annotated[Principal, Depends(get_current_principal)],
        engine: Annotated[Engine, Depends(get_engine)],
    ) -> Org:
        with tenant_session(user_id=principal.user_id, org_id=org_id, engine=engine) as session:
            ...

DSNs embed credentials, so they are never logged; SQLAlchemy URLs are rendered with the password
hidden and engines are created with ``hide_parameters=True``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings

CURRENT_USER_SETTING = "app.current_user_id"
CURRENT_ORG_SETTING = "app.current_org_id"
# Verified (iss, sub) of the caller. Lets the provisioner upsert/look up its own ``users`` row by
# external identity before the internal id is known (RLS on ``users``; see migration 0001).
CURRENT_ISSUER_SETTING = "app.current_issuer"
CURRENT_SUBJECT_SETTING = "app.current_subject"

_SET_CONTEXT = text(
    "SELECT set_config(:user_key, :user_id, true), set_config(:org_key, :org_id, true)"
)

_lock = threading.Lock()
_engines: dict[str, Engine] = {}


def create_db_engine(url: str, *, pool_size: int = 5, max_overflow: int = 5) -> Engine:
    """Sync SQLAlchemy engine on psycopg 3 with a small, self-healing pool."""
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=1800,
        hide_parameters=True,
    )


def get_engine(settings: Annotated[Settings, Depends(get_settings)]) -> Engine:
    """Process-wide engine for the app role, cached per URL. FastAPI dependency."""
    url = settings.sqlalchemy_url
    with _lock:
        engine = _engines.get(url)
        if engine is None:
            engine = create_db_engine(url)
            _engines[url] = engine
        return engine


def dispose_engines() -> None:
    """Close every cached pool (app shutdown, tests)."""
    with _lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=True, expire_on_commit=False)


def set_tenant_context(session: Session, *, user_id: UUID | None, org_id: UUID | None) -> None:
    """Set transaction-local RLS context on the session's current transaction."""
    session.execute(
        _SET_CONTEXT,
        {
            "user_key": CURRENT_USER_SETTING,
            "user_id": "" if user_id is None else str(user_id),
            "org_key": CURRENT_ORG_SETTING,
            "org_id": "" if org_id is None else str(org_id),
        },
    )


def set_identity_context(session: Session, *, issuer: str, subject: str) -> None:
    """Set the verified external identity (transaction-local). Only call with verified claims."""
    session.execute(
        text("SELECT set_config(:ik, :iss, true), set_config(:sk, :sub, true)"),
        {
            "ik": CURRENT_ISSUER_SETTING,
            "iss": issuer,
            "sk": CURRENT_SUBJECT_SETTING,
            "sub": subject,
        },
    )


@contextmanager
def tenant_session(
    *,
    user_id: UUID | None,
    org_id: UUID | None,
    engine: Engine | None = None,
) -> Iterator[Session]:
    """One transaction with RLS context set; commits on success, rolls back on any error.

    ``engine`` defaults to the app-role engine from the current settings.
    """
    bound = engine if engine is not None else get_engine(get_settings())
    session = make_sessionmaker(bound)()
    try:
        with session.begin():
            set_tenant_context(session, user_id=user_id, org_id=org_id)
            yield session
    finally:
        session.close()


class TenantSessionFactory:
    """Request-scoped factory handed to routes: open a tenant transaction once the org is known.

    Obtain with ``Depends(get_tenant_session_factory)``; call
    ``with factory(user_id=principal.user_id, org_id=org_id) as session: ...``. Routes may
    open more than one transaction (e.g. a denial audit event written in its own transaction).
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def __call__(self, *, user_id: UUID | None, org_id: UUID | None) -> Iterator[Session]:
        with tenant_session(user_id=user_id, org_id=org_id, engine=self.engine) as session:
            yield session


def get_tenant_session_factory(
    engine: Annotated[Engine, Depends(get_engine)],
) -> TenantSessionFactory:
    """FastAPI dependency. Override in tests via ``app.dependency_overrides``."""
    return TenantSessionFactory(engine)
