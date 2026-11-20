"""Fixtures of the M05 graph-build integration tests: real PostgreSQL + RLS (migration 0008) through
the real app, the worker role, and a graph store that is either the in-memory fake or a REAL
RushDB 3.0.10, selected per test.

::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m05-a1 POSTGRES_HOST_PORT=35970 REDIS_HOST_PORT=36970 \\
        RUSHDB_HTTP_HOST_PORT=33351 bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m05_graph"

Compose projects: ``ukos-m05-a1`` (postgres), ``ukos-m05-a1-rushdb``. ``UKOS_TEST_KEEP_STACK=1``
keeps them (default: ``down -v``).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m05-a1")
os.environ.setdefault("POSTGRES_HOST_PORT", "35970")
os.environ.setdefault("REDIS_HOST_PORT", "36970")
os.environ.setdefault("RUSHDB_HTTP_HOST_PORT", "33351")

REPO_ROOT = Path(__file__).resolve().parents[3]
_HERE = Path(__file__).resolve().parent
_INT = REPO_ROOT / "tests" / "integration"
for extra in (
    _HERE,
    _INT / "m01_foundation",
    _INT / "m01_tenancy",
    _INT / "m02_catalog",
    _INT / "m02_foundation",
    _INT / "m03_foundation",
    _INT / "m04_retrieval",
    REPO_ROOT / "tests" / "infrastructure",
):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def _load_foundation_fixtures() -> Any:
    spec = importlib.util.spec_from_file_location(
        "m01_foundation_fixtures_graph", _INT / "m01_foundation" / "conftest.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_foundation = _load_foundation_fixtures()
stack = _foundation.stack
admin_engine = _foundation.admin_engine
app_engine = _foundation.app_engine
alembic_config = _foundation.alembic_config
migrated = _foundation.migrated
_db_url = _foundation._url

from catalog_support import PRESIGN_TTL, World, build_world
from compose_support import ComposeProject, compose_file, wait_until
from fastapi.testclient import TestClient
from m03_support import make_url
from pydantic import SecretStr
from ret_support import Rig, RigHolder, purge_tenant
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL
from tenancy_support import Api, FakeVerifier

from app.core.config import Settings
from app.core.db import dispose_engines
from app.core.security import get_token_verifier
from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.infrastructure.rushdb.graph import RushDbGraphRepository
from app.infrastructure.rushdb.memory import InMemoryGraphRepository
from app.infrastructure.rushdb.settings import RushDbSettings
from app.main import create_app
from app.modules.documents.versions import get_presign_ttl
from app.modules.graph.router_review import get_graph_repository
from app.modules.retrieval.settings import SearchSettings, get_search_settings
from app.modules.retrieval.wiring import (
    get_artifact_store,
    get_embedder,
    get_knowledge_repository,
    get_search_limiter,
)
from app.modules.storage.deps import get_object_store

RUSHDB_PORT = os.environ["RUSHDB_HTTP_HOST_PORT"]
BASE = os.environ["UKOS_TEST_COMPOSE_PROJECT"]


@pytest.fixture(scope="session")
def rushdb_settings() -> Iterator[RushDbSettings]:
    project = ComposeProject(
        [compose_file("rushdb.yml")],
        project=BASE + "-rushdb",
        extra_env={"RUSHDB_HTTP_HOST_PORT": RUSHDB_PORT},
    )
    project.down(volumes=True)
    try:
        project.up("rushdb", wait=True)
        url = f"http://127.0.0.1:{RUSHDB_PORT}"
        wait_until(
            lambda: httpx.get(f"{url}/ready", timeout=3.0).status_code == 200,
            timeout=60,
            description="RushDB /ready",
        )
        yield RushDbSettings(RUSHDB_URL=url, RUSHDB_TOKEN=SecretStr(project.env["RUSHDB_TOKEN"]))
    finally:
        if os.environ.get("UKOS_TEST_KEEP_STACK") != "1":
            project.down(volumes=True)


@pytest.fixture(scope="session")
def app_db_url(stack: ComposeProject, migrated: None) -> URL:
    url: URL = _db_url(stack, app=True)
    return url


@pytest.fixture(scope="session")
def worker_engine(stack: ComposeProject, migrated: None) -> Iterator[Engine]:
    engine = create_engine(make_url(stack, "worker"), pool_pre_ping=True, pool_size=4)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def graph_settings(app_db_url: URL) -> Settings:
    return Settings(
        DATABASE_URL=SecretStr(app_db_url.render_as_string(hide_password=False)),
        GRAPH_ENABLED=True,
    )


@pytest.fixture(scope="session")
def verifier() -> FakeVerifier:
    return FakeVerifier()


@pytest.fixture(scope="session")
def holder() -> RigHolder:
    return RigHolder()


@pytest.fixture(scope="session")
def client(
    graph_settings: Settings, verifier: FakeVerifier, holder: RigHolder
) -> Iterator[TestClient]:
    app = create_app(graph_settings)
    app.dependency_overrides[get_token_verifier] = lambda: verifier
    app.dependency_overrides[get_object_store] = lambda: InMemoryObjectStore()
    app.dependency_overrides[get_presign_ttl] = lambda: PRESIGN_TTL
    app.dependency_overrides[get_knowledge_repository] = lambda: holder.current.repo
    app.dependency_overrides[get_graph_repository] = lambda: holder.current.repo
    app.dependency_overrides[get_embedder] = lambda: holder.current.embedder
    app.dependency_overrides[get_artifact_store] = lambda: holder.current.artifacts
    app.dependency_overrides[get_search_limiter] = lambda: holder.current.limiter
    app.dependency_overrides[get_search_settings] = lambda: SearchSettings()
    with TestClient(app) as test_client:
        yield test_client
    dispose_engines()


@pytest.fixture
def api(client: TestClient, verifier: FakeVerifier, admin_engine: Engine) -> Api:
    return Api(client, verifier, admin_engine)


@pytest.fixture
def world(api: Api) -> World:
    return build_world(api)


@pytest.fixture(params=["memory", "real"])
def rig(
    request: pytest.FixtureRequest,
    holder: RigHolder,
    admin_engine: Engine,
    app_engine: Engine,
    world: World,
) -> Iterator[Rig]:
    """The graph store under test (both implementations)."""
    repo: Any
    rushdb: RushDbSettings | None = None
    if request.param == "real":
        rushdb = request.getfixturevalue("rushdb_settings")
        assert rushdb is not None
        repo = RushDbGraphRepository(rushdb)
    else:
        repo = InMemoryGraphRepository()
    current = Rig(admin_engine, app_engine, repo, real=rushdb is not None)
    holder.current = current
    yield current
    if rushdb is not None:
        for org in current.orgs | {world.org_id, world.foreign.org_id}:
            purge_tenant(rushdb.RUSHDB_URL, rushdb.RUSHDB_TOKEN.get_secret_value(), f"org:{org}")


@pytest.fixture
def mrig(holder: RigHolder, admin_engine: Engine, app_engine: Engine) -> Rig:
    """Memory-only rig for the route tests."""
    current = Rig(admin_engine, app_engine, InMemoryGraphRepository(), real=False)
    holder.current = current
    return current
