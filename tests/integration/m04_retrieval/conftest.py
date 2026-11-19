"""Fixtures of the M04 retrieval integration tests: real PostgreSQL + RLS through the real app,
and both knowledge indexes (the in-memory fake and a REAL RushDB 3.0.10), selected per test.

::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m04-ret POSTGRES_HOST_PORT=35995 REDIS_HOST_PORT=36995 \\
        RUSHDB_HTTP_HOST_PORT=33331 bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m04_retrieval"

Compose projects: ``ukos-m04-ret`` (postgres), ``ukos-m04-ret-rushdb``. ``UKOS_TEST_KEEP_STACK=1``
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

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m04-ret")
os.environ.setdefault("POSTGRES_HOST_PORT", "35995")
os.environ.setdefault("REDIS_HOST_PORT", "36995")
os.environ.setdefault("RUSHDB_HTTP_HOST_PORT", "33331")

REPO_ROOT = Path(__file__).resolve().parents[3]
_HERE = Path(__file__).resolve().parent
_FOUNDATION_DIR = REPO_ROOT / "tests" / "integration" / "m01_foundation"
_TENANCY_DIR = REPO_ROOT / "tests" / "integration" / "m01_tenancy"
_CATALOG_DIR = REPO_ROOT / "tests" / "integration" / "m02_catalog"
_M02_DIR = REPO_ROOT / "tests" / "integration" / "m02_foundation"
_M03_DIR = REPO_ROOT / "tests" / "integration" / "m03_foundation"
_INFRA_DIR = REPO_ROOT / "tests" / "infrastructure"
for extra in (_HERE, _FOUNDATION_DIR, _TENANCY_DIR, _CATALOG_DIR, _M02_DIR, _M03_DIR, _INFRA_DIR):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def _load_foundation_fixtures() -> Any:
    spec = importlib.util.spec_from_file_location(
        "m01_foundation_fixtures_retrieval", _FOUNDATION_DIR / "conftest.py"
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
from pydantic import SecretStr
from ret_support import Rig, RigHolder, purge_tenant
from sqlalchemy import Engine
from sqlalchemy.engine import URL
from tenancy_support import Api, FakeVerifier

from app.core.config import Settings
from app.core.db import dispose_engines
from app.core.security import get_token_verifier
from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.infrastructure.rushdb.memory import InMemoryKnowledgeRepository
from app.infrastructure.rushdb.repository import RushDbKnowledgeRepository
from app.infrastructure.rushdb.settings import RushDbSettings
from app.main import create_app
from app.modules.documents.versions import get_presign_ttl
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
def verifier() -> FakeVerifier:
    return FakeVerifier()


@pytest.fixture(scope="session")
def holder() -> RigHolder:
    return RigHolder()


@pytest.fixture(scope="session")
def client(app_db_url: URL, verifier: FakeVerifier, holder: RigHolder) -> Iterator[TestClient]:
    settings = Settings(DATABASE_URL=SecretStr(app_db_url.render_as_string(hide_password=False)))
    app = create_app(settings)
    app.dependency_overrides[get_token_verifier] = lambda: verifier
    app.dependency_overrides[get_object_store] = lambda: InMemoryObjectStore()
    app.dependency_overrides[get_presign_ttl] = lambda: PRESIGN_TTL
    app.dependency_overrides[get_knowledge_repository] = lambda: holder.current.repo
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
    """The index under test (both implementations) with a fake embedder and artifact store."""
    repo: Any
    rushdb: RushDbSettings | None = None
    if request.param == "real":
        rushdb = request.getfixturevalue("rushdb_settings")
        assert rushdb is not None
        repo = RushDbKnowledgeRepository(rushdb)
    else:
        repo = InMemoryKnowledgeRepository()
    current = Rig(admin_engine, app_engine, repo, real=rushdb is not None)
    holder.current = current
    yield current
    if rushdb is not None:
        for org in current.orgs | {world.org_id, world.foreign.org_id}:
            purge_tenant(rushdb.RUSHDB_URL, rushdb.RUSHDB_TOKEN.get_secret_value(), f"org:{org}")
