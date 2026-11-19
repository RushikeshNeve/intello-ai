"""DB-backed fixtures for M04 persistence (migration 0007: chat tables, AI policy, definers).

Boots ``infrastructure/compose/data.yml`` (postgres only) under its own Compose project and
migrates it with Alembic as the admin role, passing the worker password through ``-x`` (the other
documented channel is the ``POSTGRES_WORKER_PASSWORD`` environment variable). Three roles are
used: the admin (seeding, introspection), ``ukos_app`` (the API) and ``ukos_worker``.

::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m04-pers POSTGRES_HOST_PORT=35996 REDIS_HOST_PORT=36996 \\
        bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m03_foundation"

``test_m03_finalize_atomic.py`` also drives the real FastAPI app (fake token verifier, in-memory
object store) against the same database. Set ``UKOS_TEST_KEEP_STACK=1`` to keep the stack.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m04-pers")
os.environ.setdefault("POSTGRES_HOST_PORT", "35996")
os.environ.setdefault("REDIS_HOST_PORT", "36996")

REPO_ROOT = Path(__file__).resolve().parents[3]
API_DIR = REPO_ROOT / "apps" / "api"
for extra in (
    Path(__file__).resolve().parent,
    API_DIR,
    REPO_ROOT / "tests" / "infrastructure",
    REPO_ROOT / "tests" / "integration" / "m01_foundation",  # m01_seed
    REPO_ROOT / "tests" / "integration" / "m01_tenancy",  # tenancy_support (FakeVerifier, Api)
    REPO_ROOT / "tests" / "integration" / "m02_foundation",  # m02_seed (via m03_support)
    REPO_ROOT / "tests" / "integration" / "m03_foundation",  # m03_support (alembic_cfg, make_url)
):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from alembic import command
from alembic.config import Config
from compose_support import ComposeProject, compose_file, require_docker, wait_until
from fastapi.testclient import TestClient
from m03_support import alembic_cfg, make_url
from m04_support import ChatWorld, seed_world
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from tenancy_support import Api, FakeVerifier

from app.core.config import Settings
from app.core.db import dispose_engines
from app.core.security import get_token_verifier
from app.main import create_app

require_docker()

PROJECT = os.environ.get("UKOS_TEST_COMPOSE_PROJECT", "ukos-m04-pers")


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
    engine = create_engine(make_url(stack, "admin"), pool_pre_ping=True)

    def ready() -> bool:
        with engine.connect() as conn:
            return conn.execute(text("SELECT 1")).scalar() == 1

    wait_until(ready, timeout=60, description="postgres (admin) connection")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def alembic_config(stack: ComposeProject) -> Iterator[Config]:
    previous = {
        k: os.environ.get(k)
        for k in ("DATABASE_MIGRATION_URL", "POSTGRES_APP_USER", "POSTGRES_WORKER_PASSWORD")
    }
    os.environ["DATABASE_MIGRATION_URL"] = make_url(stack, "admin").render_as_string(
        hide_password=False
    )
    os.environ["POSTGRES_APP_USER"] = stack.env["POSTGRES_APP_USER"]
    os.environ.pop("POSTGRES_WORKER_PASSWORD", None)  # the password must arrive through -x
    try:
        yield alembic_cfg(stack)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="session")
def migrated(alembic_config: Config, admin_engine: Engine) -> None:
    command.upgrade(alembic_config, "head")


@pytest.fixture(scope="session")
def app_engine(stack: ComposeProject, migrated: None) -> Iterator[Engine]:
    engine = create_engine(make_url(stack, "app"), pool_pre_ping=True, pool_size=4)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def worker_engine(stack: ComposeProject, migrated: None) -> Iterator[Engine]:
    engine = create_engine(make_url(stack, "worker"), pool_pre_ping=True, pool_size=6)
    yield engine
    engine.dispose()


# -- the real API on the same database -------------------------------------------------------


@pytest.fixture(scope="session")
def verifier() -> FakeVerifier:
    return FakeVerifier()


@pytest.fixture(scope="session")
def base_settings(stack: ComposeProject, migrated: None) -> Settings:
    url = make_url(stack, "app").render_as_string(hide_password=False)
    return Settings(DATABASE_URL=SecretStr(url))


@pytest.fixture(scope="session")
def client(base_settings: Settings, verifier: FakeVerifier) -> Iterator[TestClient]:
    app = create_app(base_settings)
    app.dependency_overrides[get_token_verifier] = lambda: verifier
    with TestClient(app) as test_client:
        yield test_client
    dispose_engines()


@pytest.fixture
def api(client: TestClient, verifier: FakeVerifier, admin_engine: Engine) -> Api:
    return Api(client, verifier, admin_engine)


@pytest.fixture(scope="session")
def world(admin_engine: Engine, migrated: None) -> ChatWorld:
    return seed_world(admin_engine)
