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

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from compose_support import ComposeProject, compose_file, require_docker, wait_until  # noqa: E402
from rls_world import World, seed_world  # noqa: E402
from sqlalchemy import Engine, create_engine, text  # noqa: E402
from sqlalchemy.engine import URL  # noqa: E402

require_docker()

PROJECT = os.environ.get("UKOS_RLS_COMPOSE_PROJECT", "ukos-m01-rls")


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

    wait_until(ready, timeout=90, description="postgres (admin) connection")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def migrated(stack: ComposeProject, admin_engine: Engine) -> None:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "migrations"))
    previous = {k: os.environ.get(k) for k in ("DATABASE_MIGRATION_URL", "POSTGRES_APP_USER")}
    os.environ["DATABASE_MIGRATION_URL"] = _url(stack, app=False).render_as_string(
        hide_password=False
    )
    os.environ["POSTGRES_APP_USER"] = stack.env["POSTGRES_APP_USER"]
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
def app_url(stack: ComposeProject) -> URL:
    return _url(stack, app=True)


@pytest.fixture(scope="session")
def app_role(stack: ComposeProject) -> str:
    return stack.env["POSTGRES_APP_USER"]


@pytest.fixture(scope="session")
def world(admin_engine: Engine, migrated: None) -> World:
    return seed_world(admin_engine)
