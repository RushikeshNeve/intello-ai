"""Fixtures of the M07 backend tests (memory, research): real PostgreSQL, the real API (in-process
TestClient), the real worker role for the research executor / chat executor (in-process) and an
in-memory knowledge index with a fake embedder. No Redis, no worker processes.

::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m07-b-int POSTGRES_HOST_PORT=35977 REDIS_HOST_PORT=36977 \\
        bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m07_backend"
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m07-b-int")
os.environ.setdefault("POSTGRES_HOST_PORT", "35977")
os.environ.setdefault("REDIS_HOST_PORT", "36977")

REPO_ROOT = Path(__file__).resolve().parents[3]
API_DIR = REPO_ROOT / "apps" / "api"
for extra in (
    Path(__file__).resolve().parent,
    API_DIR,
    REPO_ROOT / "tests" / "infrastructure",
    REPO_ROOT / "tests" / "integration" / "m01_foundation",
    REPO_ROOT / "tests" / "integration" / "m01_tenancy",
    REPO_ROOT / "tests" / "integration" / "m02_foundation",
    REPO_ROOT / "tests" / "integration" / "m02_catalog",
    REPO_ROOT / "tests" / "integration" / "m03_foundation",
    REPO_ROOT / "tests" / "integration" / "m03_runtime",
    REPO_ROOT / "tests" / "integration" / "m04_retrieval",
    REPO_ROOT / "tests" / "integration" / "m04_chat",
):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from alembic import command
from alembic.config import Config
from catalog_support import World, build_world
from compose_support import ComposeProject, compose_file, require_docker, wait_until
from fastapi import FastAPI
from fastapi.testclient import TestClient
from m03_support import alembic_cfg, make_url
from pydantic import SecretStr
from ret_support import Rig
from sqlalchemy import Engine, create_engine, text
from tenancy_support import Api, FakeVerifier

from app.core.config import Settings
from app.core.db import dispose_engines
from app.core.security import get_token_verifier
from app.infrastructure.rushdb.memory import InMemoryKnowledgeRepository
from app.main import create_app

require_docker()

PROJECT = os.environ["UKOS_TEST_COMPOSE_PROJECT"]


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
    engine = create_engine(make_url(stack, "admin"), pool_pre_ping=True, pool_size=6)

    def ready() -> bool:
        with engine.connect() as conn:
            return conn.execute(text("SELECT 1")).scalar() == 1

    wait_until(ready, timeout=60, description="postgres (admin)")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def migrated(stack: ComposeProject, admin_engine: Engine) -> None:
    previous = {k: os.environ.get(k) for k in ("DATABASE_MIGRATION_URL", "POSTGRES_APP_USER")}
    os.environ["DATABASE_MIGRATION_URL"] = make_url(stack, "admin").render_as_string(
        hide_password=False
    )
    os.environ["POSTGRES_APP_USER"] = stack.env["POSTGRES_APP_USER"]
    cfg: Config = alembic_cfg(stack)
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
    engine = create_engine(make_url(stack, "app"), pool_pre_ping=True, pool_size=6)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def worker_engine(stack: ComposeProject, migrated: None) -> Iterator[Engine]:
    engine = create_engine(make_url(stack, "worker"), pool_pre_ping=True, pool_size=8)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def verifier() -> FakeVerifier:
    return FakeVerifier()


@pytest.fixture(scope="session")
def base_settings(stack: ComposeProject, migrated: None) -> Settings:
    url = make_url(stack, "app").render_as_string(hide_password=False)
    return Settings(DATABASE_URL=SecretStr(url))


@pytest.fixture(scope="session")
def app(base_settings: Settings, verifier: FakeVerifier) -> FastAPI:
    application = create_app(base_settings)
    application.dependency_overrides[get_token_verifier] = lambda: verifier
    return application


@pytest.fixture(scope="session")
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    dispose_engines()


@pytest.fixture
def api(client: TestClient, verifier: FakeVerifier, admin_engine: Engine) -> Api:
    return Api(client, verifier, admin_engine)


@pytest.fixture
def world(api: Api) -> World:
    return build_world(api)


@pytest.fixture(scope="session")
def rig(admin_engine: Engine, app_engine: Engine) -> Rig:
    return Rig(admin_engine, app_engine, InMemoryKnowledgeRepository(), real=False)
