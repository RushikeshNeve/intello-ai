"""Database access of the worker processes: the ``ukos_worker`` role (M03 contract §4).

Two access shapes only:

* :func:`system_connection` runs the SECURITY DEFINER functions that look across orgs (claim, reap,
  outbox, reconcile). No org context is set (none is needed, and none is accepted for other
  tables: RLS is fail-closed).
* :func:`worker_session` sets the transaction-local ``app.current_org_id`` of one job's org; every
  statement then runs under the normal RLS policies.

Engines are created lazily and keyed by process id, so a prefork Celery child never reuses a
connection pool inherited from its parent. DSNs are never logged.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

from sqlalchemy import Connection, Engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.db import create_db_engine, make_sessionmaker, set_tenant_context
from app.workers.pipeline_port import WorkerConfigError

_lock = threading.Lock()
_engines: dict[tuple[int, str], Engine] = {}


def get_worker_engine(settings: Settings) -> Engine:
    """Process-wide engine for the worker role (``DATABASE_WORKER_URL``)."""
    url = settings.sqlalchemy_worker_url
    if url is None:
        raise WorkerConfigError("DATABASE_WORKER_URL is not set: workers connect as ukos_worker")
    key = (os.getpid(), url)
    with _lock:
        engine = _engines.get(key)
        if engine is None:
            engine = create_db_engine(url, pool_size=5, max_overflow=5)
            _engines[key] = engine
        return engine


def dispose_worker_engines() -> None:
    with _lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()


@contextmanager
def system_connection(engine: Engine) -> Iterator[Connection]:
    """One transaction without org context (for the definer functions); commits on success."""
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            yield conn
        except BaseException:
            trans.rollback()
            raise
        else:
            trans.commit()


@contextmanager
def worker_session(engine: Engine, org_id: uuid.UUID) -> Iterator[Session]:
    """One transaction under ``app.current_org_id = org_id``; commits on success."""
    session = make_sessionmaker(engine)()
    try:
        with session.begin():
            set_tenant_context(session, user_id=None, org_id=org_id)
            yield session
    finally:
        session.close()


def session_factory_for(engine: Engine) -> Callable[[uuid.UUID], AbstractContextManager[Session]]:
    def factory(org_id: uuid.UUID) -> AbstractContextManager[Session]:
        return worker_session(engine, org_id)

    return factory
