"""Self-contained fixtures for the adversarial PostgreSQL RLS suite (M01.03.02).

Boots ``infrastructure/compose/data.yml`` (postgres only) under its own Compose project
``ukos-m01-rls``, applies the Alembic migrations as the admin role, seeds two complete tenants
(org A, org B) plus cross-cutting users as the admin role, and runs every attack as the
least-privilege app role ``ukos_app``. The stack is removed with ``down -v`` at the end.

One command (from the repo root)::

    uv run --no-project --with pytest --with "psycopg[binary]" --with sqlalchemy --with alembic \\
        --with fastapi --with pydantic-settings python -m pytest -q -p no:cacheprovider \\
        tests/security/postgres_rls

Defaults: ``POSTGRES_HOST_PORT=25544``, ``REDIS_HOST_PORT=26544`` (override via env).
``UKOS_RLS_COMPOSE_PROJECT`` overrides the project name; ``UKOS_TEST_KEEP_STACK=1`` keeps it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
API_DIR = REPO_ROOT / "apps" / "api"
HERE = Path(__file__).resolve().parent
for extra in (HERE, API_DIR, REPO_ROOT / "tests" / "infrastructure"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

# Isolated host ports for this suite (compose interpolation and stack.env both read os.environ).
os.environ.setdefault("POSTGRES_HOST_PORT", "25544")
os.environ.setdefault("REDIS_HOST_PORT", "26544")

from alembic import command
from alembic.config import Config
from compose_support import ComposeProject, compose_file, require_docker, wait_until
from rls_chat_world import ChatWorld, seed_chat_world
from rls_connector_world import ConnectorWorld, seed_connector_world
from rls_docs_world import DocWorld, seed_doc_world
from rls_m07_world import M07World, seed_m07_world
from rls_world import World, seed_world
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

require_docker()

PROJECT = os.environ.get("UKOS_RLS_COMPOSE_PROJECT", "ukos-m01-rls")


def _url(stack: ComposeProject, *, app: bool, worker: bool = False) -> URL:
    env = stack.env
    if worker:
        user, password = env["POSTGRES_WORKER_USER"], env["POSTGRES_WORKER_PASSWORD"]
    elif app:
        user, password = env["POSTGRES_APP_USER"], env["POSTGRES_APP_PASSWORD"]
    else:
        user, password = env["POSTGRES_USER"], env["POSTGRES_PASSWORD"]
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host="127.0.0.1",
        port=int(env["POSTGRES_HOST_PORT"]),
        database=env["POSTGRES_DB"],
    )


@pytest.fixture(scope="session")
def stack() -> Iterator[ComposeProject]:
    project = ComposeProject([compose_file("data.yml")], project=PROJECT)
    project.down(volumes=True)
    try:
        project.up("postgres", wait=True)
        yield project
    finally:
        if os.environ.get("UKOS_TEST_KEEP_STACK") != "1":
            project.down(volumes=True)


@pytest.fixture(scope="session")
def admin_engine(stack: ComposeProject) -> Iterator[Engine]:
    engine = create_engine(_url(stack, app=False), pool_pre_ping=True)

    def ready() -> bool:
        with engine.connect() as conn:
            return conn.execute(text("SELECT 1")).scalar() == 1

    wait_until(ready, timeout=90, description="postgres (admin) connection")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def migrated(stack: ComposeProject, admin_engine: Engine) -> None:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "migrations"))
    previous = {
        k: os.environ.get(k)
        for k in ("DATABASE_MIGRATION_URL", "POSTGRES_APP_USER", "POSTGRES_WORKER_PASSWORD")
    }
    os.environ["DATABASE_MIGRATION_URL"] = _url(stack, app=False).render_as_string(
        hide_password=False
    )
    os.environ["POSTGRES_APP_USER"] = stack.env["POSTGRES_APP_USER"]
    # Migration 0005 creates the worker role from this password (M03 contract §4).
    os.environ["POSTGRES_WORKER_PASSWORD"] = stack.env["POSTGRES_WORKER_PASSWORD"]
    try:
        command.upgrade(cfg, "head")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="session")
def app_engine(stack: ComposeProject, migrated: None) -> Iterator[Engine]:
    engine = create_engine(_url(stack, app=True), pool_pre_ping=True, pool_size=2)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def worker_engine(stack: ComposeProject, migrated: None) -> Iterator[Engine]:
    """The M03 ingestion worker role (``ukos_worker``): LOGIN NOSUPERUSER NOBYPASSRLS."""
    engine = create_engine(_url(stack, app=False, worker=True), pool_pre_ping=True, pool_size=2)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def worker_url(stack: ComposeProject) -> URL:
    return _url(stack, app=False, worker=True)


@pytest.fixture(scope="session")
def worker_role(stack: ComposeProject) -> str:
    return str(stack.env["POSTGRES_WORKER_USER"])


@pytest.fixture(scope="session")
def app_url(stack: ComposeProject) -> URL:
    return _url(stack, app=True)


@pytest.fixture(scope="session")
def app_role(stack: ComposeProject) -> str:
    return stack.env["POSTGRES_APP_USER"]


@pytest.fixture(scope="session")
def world(admin_engine: Engine, migrated: None) -> World:
    return seed_world(admin_engine)


@pytest.fixture(scope="session")
def docs(admin_engine: Engine, world: World) -> DocWorld:
    """Per-tenant document/ingestion rows for the M02 + M03 tables (admin-seeded)."""
    return seed_doc_world(admin_engine, world)


@pytest.fixture(scope="session")
def chat(admin_engine: Engine, world: World) -> ChatWorld:
    """Per-tenant chat / AI-policy rows for the M04 tables (admin-seeded; migration 0007)."""
    return seed_chat_world(admin_engine, world)


@pytest.fixture(scope="session")
def connectors(admin_engine: Engine, world: World) -> ConnectorWorld:
    """Per-tenant connector rows for the M06 tables (admin-seeded; migration 0009)."""
    return seed_connector_world(admin_engine, world)


@pytest.fixture(scope="session")
def m07(admin_engine: Engine, world: World) -> M07World:
    """Per-tenant memory / research rows for the M07 tables (admin-seeded; migration 0010)."""
    return seed_m07_world(admin_engine, world)


@pytest.fixture(scope="session")
def engines(app_engine: Engine, worker_engine: Engine) -> dict[str, Engine]:
    """Role name (``app`` / ``worker``) -> engine."""
    return {"app": app_engine, "worker": worker_engine}


@pytest.fixture(scope="session")
def role_names(app_role: str, worker_role: str) -> dict[str, str]:
    """Role key (``app`` / ``worker``) -> PostgreSQL role name."""
    return {"app": app_role, "worker": worker_role}
