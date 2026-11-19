"""DB-backed fixtures for the M01 foundation (schema, constraints, RLS smoke).

Boots ``infrastructure/compose/data.yml`` under its own Compose project and migrates it with
Alembic as the admin role. Host ports come from ``infrastructure/env/.env.example`` overlaid by
same-named OS environment variables (``tests/infrastructure/compose_support.py``), so isolated
stacks can run in parallel::

    cd apps/api
    UKOS_TEST_COMPOSE_PROJECT=ukos-m01-foundation POSTGRES_HOST_PORT=25532 REDIS_HOST_PORT=26479 \\
        uv run --no-sync python -m pytest -q ../../tests/integration/m01_foundation

Set ``UKOS_TEST_KEEP_STACK=1`` to leave the stack running afterwards (default: ``down -v``).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
API_DIR = REPO_ROOT / "apps" / "api"
for extra in (API_DIR, REPO_ROOT / "tests" / "infrastructure"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from compose_support import ComposeProject, compose_file, require_docker, wait_until  # noqa: E402
from sqlalchemy import Engine, create_engine, text  # noqa: E402
from sqlalchemy.engine import URL  # noqa: E402

require_docker()

PROJECT = os.environ.get("UKOS_TEST_COMPOSE_PROJECT", "ukos-m01-foundation")


def _url(stack: ComposeProject, *, app: bool) -> URL:
    env = stack.env
    return URL.create(
        "postgresql+psycopg",
        username=env["POSTGRES_APP_USER"] if app else env["POSTGRES_USER"],
        password=env["POSTGRES_APP_PASSWORD"] if app else env["POSTGRES_PASSWORD"],
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

    wait_until(ready, timeout=60, description="postgres (admin) connection")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_engine(stack: ComposeProject, migrated: None) -> Iterator[Engine]:
    engine = create_engine(_url(stack, app=True), pool_pre_ping=True, pool_size=2)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def alembic_config(stack: ComposeProject) -> Iterator[Config]:
    cfg = Config(str(API_DIR / "alembic.ini"))
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
        yield cfg
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="session")
def migrated(alembic_config: Config, admin_engine: Engine) -> None:
    command.upgrade(alembic_config, "head")
