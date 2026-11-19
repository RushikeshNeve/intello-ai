"""Fixtures for the worker-runtime tests: real Postgres + real Redis, real worker processes.

::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m03-rt POSTGRES_HOST_PORT=35991 REDIS_HOST_PORT=36991 \\
        bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m03_runtime"
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m03-rt")
os.environ.setdefault("POSTGRES_HOST_PORT", "35991")
os.environ.setdefault("REDIS_HOST_PORT", "36991")

REPO_ROOT = Path(__file__).resolve().parents[3]
API_DIR = REPO_ROOT / "apps" / "api"
for extra in (
    Path(__file__).resolve().parent,
    API_DIR,
    REPO_ROOT / "tests" / "infrastructure",
    REPO_ROOT / "tests" / "integration" / "m01_foundation",
    REPO_ROOT / "tests" / "integration" / "m02_foundation",
    REPO_ROOT / "tests" / "integration" / "m03_foundation",
):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import redis
from alembic import command
from alembic.config import Config
from compose_support import ComposeProject, compose_file, require_docker, wait_until
from m03_support import Scope, alembic_cfg, make_url, seed_scope
from sqlalchemy import Engine, create_engine, text

require_docker()

PROJECT = os.environ["UKOS_TEST_COMPOSE_PROJECT"]


@pytest.fixture(scope="session")
def stack() -> Iterator[ComposeProject]:
    project = ComposeProject([compose_file("data.yml")], project=PROJECT)
    project.down(volumes=True)
    try:
        project.up("postgres", "redis", wait=True)
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
    engine = create_engine(make_url(stack, "app"), pool_pre_ping=True, pool_size=4)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def worker_engine(stack: ComposeProject, migrated: None) -> Iterator[Engine]:
    engine = create_engine(make_url(stack, "worker"), pool_pre_ping=True, pool_size=8)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def redis_client(stack: ComposeProject) -> redis.Redis:
    from runtime_support import broker_url

    return redis.Redis.from_url(broker_url(stack))


@pytest.fixture
def scope(admin_engine: Engine, migrated: None) -> Scope:
    return seed_scope(admin_engine)


@pytest.fixture
def fake_dir(tmp_path: Path) -> Path:
    d = tmp_path / "fake"
    d.mkdir()
    return d


@pytest.fixture
def clean_broker(redis_client: redis.Redis) -> None:
    redis_client.flushdb()
