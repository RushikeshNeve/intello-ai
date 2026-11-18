"""DB-backed fixtures for the M02 foundation (migration 0003, constraints, immutability, RLS).

Boots ``infrastructure/compose/data.yml`` under its own Compose project and migrates it with
Alembic as the admin role. Host ports come from ``infrastructure/env/.env.example`` overlaid by
same-named OS environment variables (``tests/infrastructure/compose_support.py``)::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m02-found POSTGRES_HOST_PORT=35901 REDIS_HOST_PORT=36901 \\
        bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m02_foundation"

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
for extra in (
    Path(__file__).resolve().parent,
    API_DIR,
    REPO_ROOT / "tests" / "infrastructure",
    REPO_ROOT / "tests" / "integration" / "m01_foundation",  # m01_seed (tenants, constraint_name)
):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from compose_support import ComposeProject, compose_file, require_docker, wait_until  # noqa: E402
from m01_seed import Tenant, seed_tenant  # noqa: E402
from sqlalchemy import Engine, create_engine, text  # noqa: E402
from sqlalchemy.engine import URL  # noqa: E402

require_docker()

PROJECT = os.environ.get("UKOS_TEST_COMPOSE_PROJECT", "ukos-m02-found")


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
    previous = {k: os.environ.get(k) for k in ("DATABASE_MIGRATION_URL", "POSTGRES_APP_USER")}
    os.environ["DATABASE_MIGRATION_URL"] = _url(stack, app=False).render_as_string(
        hide_password=False
    )
    os.environ["POSTGRES_APP_USER"] = stack.env["POSTGRES_APP_USER"]
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


@pytest.fixture
def tenant(admin_engine: Engine, migrated: None) -> Tenant:
    """A fresh org + owner + workspace (admin-seeded), isolated per test."""
    return seed_tenant(admin_engine, documents=False)


@pytest.fixture
def other_tenant(admin_engine: Engine, migrated: None) -> Tenant:
    return seed_tenant(admin_engine, documents=False)
