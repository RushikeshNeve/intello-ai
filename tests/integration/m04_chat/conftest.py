"""Fixtures of the M04 chat runtime tests (M04.02.04): real PostgreSQL + real Redis, the real API
(in-process TestClient and a live uvicorn thread for SSE) and, for the end-to-end modules, REAL
Celery worker and dispatcher processes with a fake index/model (``chat_fake_runtime``).

::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m04-chat POSTGRES_HOST_PORT=35998 REDIS_HOST_PORT=36998 \\
        bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m04_chat"
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m04-chat")
os.environ.setdefault("POSTGRES_HOST_PORT", "35998")
os.environ.setdefault("REDIS_HOST_PORT", "36998")

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
):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import redis
import uvicorn
from alembic import command
from alembic.config import Config
from catalog_support import World, build_world
from chat_support import RecordingRepo, chat_env, drain_chat_backlog
from compose_support import ComposeProject, compose_file, require_docker, wait_until
from fastapi import FastAPI
from fastapi.testclient import TestClient
from m03_support import alembic_cfg, make_url
from pydantic import SecretStr
from ret_support import Rig
from runtime_support import Proc, broker_url, start_dispatcher, start_worker
from sqlalchemy import Engine, create_engine, text
from tenancy_support import Api, FakeVerifier

from app.core.config import Settings, get_settings
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
        project.up("postgres", "redis", wait=True)
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
def redis_client(stack: ComposeProject) -> redis.Redis:
    return redis.Redis.from_url(broker_url(stack))


# -- the real API: in-process client and a live server for SSE ------------------------------------


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


@pytest.fixture(scope="session")
def live_url(app: FastAPI) -> Iterator[str]:
    """The same app served by uvicorn in a thread (TestClient cannot stream an open SSE body)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, name="uvicorn-live", daemon=True)
    thread.start()
    wait_until(lambda: server.started, timeout=30, interval=0.1, description="uvicorn started")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=15)


@pytest.fixture
def api(client: TestClient, verifier: FakeVerifier, admin_engine: Engine) -> Api:
    return Api(client, verifier, admin_engine)


@pytest.fixture
def world(api: Api) -> World:
    return build_world(api)


@pytest.fixture
def settings_override(app: FastAPI, base_settings: Settings) -> Iterator[object]:
    """``settings_override(CHAT_MAX_RUNS_PER_MIN=1)`` runs the app with other settings."""

    def apply(**values: object) -> Settings:
        changed = base_settings.model_copy(update=values)
        app.dependency_overrides[get_settings] = lambda: changed
        return changed

    yield apply
    app.dependency_overrides[get_settings] = lambda: base_settings


# -- the knowledge index and the fake worker directory ---------------------------------------------


@pytest.fixture(scope="session")
def fake_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("chatfake")


@pytest.fixture(scope="session")
def rig(admin_engine: Engine, app_engine: Engine, fake_dir: Path) -> Rig:
    repo = RecordingRepo(InMemoryKnowledgeRepository(), fake_dir / "corpus.jsonl")
    return Rig(admin_engine, app_engine, repo, real=False)


@pytest.fixture(autouse=True)
def _clean_rig_hooks(request: pytest.FixtureRequest) -> Iterator[None]:
    yield
    if "rig" in request.fixturenames:
        current = request.getfixturevalue("rig")
        current.repo.fail.clear()
        current.repo.after.clear()
        current.repo.calls.clear()


# -- worker and dispatcher processes (end-to-end modules) ------------------------------------


class Runtime:
    def __init__(self, worker: Proc, dispatcher: Proc, env: dict[str, str]) -> None:
        self.worker = worker
        self.dispatcher = dispatcher
        self.env = env


@pytest.fixture(scope="module")
def runtime(
    stack: ComposeProject,
    fake_dir: Path,
    migrated: None,
    admin_engine: Engine,
    redis_client: redis.Redis,
) -> Iterator[Runtime]:
    drain_chat_backlog(admin_engine, redis_client)
    env = chat_env(stack, fake_dir)
    worker = start_worker(env, fake_dir, concurrency=2)
    dispatcher = start_dispatcher(env, fake_dir)
    try:
        yield Runtime(worker, dispatcher, env)
    finally:
        dispatcher.stop()
        worker.stop()
    time.sleep(0.1)
