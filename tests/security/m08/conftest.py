"""Fixtures of the M08.01 security suites: real PostgreSQL + RLS through the real app (in-process
TestClient), the in-memory knowledge index (and a REAL RushDB for one test), a fake graph read
repository and a fake embedder. Built on the M04 retrieval fixtures.

::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m08-a1 POSTGRES_HOST_PORT=35981 REDIS_HOST_PORT=36981 \\
        RUSHDB_HTTP_HOST_PORT=33371 bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/security/m08"
"""

from __future__ import annotations

import base64
import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m08-a1")
os.environ.setdefault("POSTGRES_HOST_PORT", "35981")
os.environ.setdefault("REDIS_HOST_PORT", "36981")
os.environ.setdefault("RUSHDB_HTTP_HOST_PORT", "33371")

REPO_ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
for _extra in (
    HERE,
    REPO_ROOT / "tests" / "integration" / "m04_chat",
    REPO_ROOT / "tests" / "integration" / "m06_connectors",
    REPO_ROOT / "tests" / "integration" / "m03_runtime",
):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

_M04 = REPO_ROOT / "tests" / "integration" / "m04_retrieval"
_spec = importlib.util.spec_from_file_location(
    "m04_retrieval_fixtures_for_m08", _M04 / "conftest.py"
)
assert _spec is not None
assert _spec.loader is not None
_m04 = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _m04
_spec.loader.exec_module(_m04)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from ret_support import Rig, purge_tenant
from sqlalchemy import Engine

from app.core.config import Settings, get_settings
from app.core.db import dispose_engines
from app.core.security import get_token_verifier
from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.infrastructure.rushdb.memory import InMemoryKnowledgeRepository
from app.infrastructure.rushdb.repository import RushDbKnowledgeRepository
from app.main import create_app
from app.modules.documents.versions import get_presign_ttl
from app.modules.graph.fake_read import FakeGraphRead
from app.modules.graph.read_settings import (
    GraphReadSettings,
    get_graph_read_settings,
)
from app.modules.graph.wiring import get_graph_read_repository
from app.modules.retrieval.settings import SearchSettings, get_search_settings
from app.modules.retrieval.wiring import (
    get_artifact_store,
    get_embedder,
    get_knowledge_repository,
    get_search_limiter,
)
from app.modules.storage.deps import get_object_store

for _name in (
    "stack",
    "admin_engine",
    "app_engine",
    "alembic_config",
    "migrated",
    "app_db_url",
    "verifier",
    "holder",
    "api",
    "world",
    "rushdb_settings",
):
    globals()[_name] = getattr(_m04, _name)


TEST_KEY = base64.b64encode(bytes(range(32))).decode()


class SettingsHolder:
    """The Settings the app resolves per request (tests swap them to rotate keys)."""

    current: Settings


class GraphHolder:
    graph: FakeGraphRead = FakeGraphRead()


@pytest.fixture(scope="session")
def gholder() -> GraphHolder:
    return GraphHolder()


@pytest.fixture(scope="session")
def sholder(app_db_url: Any) -> SettingsHolder:
    holder_ = SettingsHolder()
    holder_.current = Settings(
        DATABASE_URL=SecretStr(app_db_url.render_as_string(hide_password=False)),
        CONNECTOR_ENCRYPTION_KEY=SecretStr(TEST_KEY),
    )
    return holder_


@pytest.fixture(scope="session")
def worker_engine(stack: Any, migrated: None) -> Iterator[Engine]:
    from m03_support import make_url
    from sqlalchemy import create_engine

    engine = create_engine(make_url(stack, "worker"), pool_pre_ping=True, pool_size=6)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app(sholder: SettingsHolder, verifier: Any, holder: Any, gholder: GraphHolder) -> FastAPI:
    application = create_app(sholder.current)
    ov = application.dependency_overrides
    ov[get_settings] = lambda: sholder.current
    ov[get_token_verifier] = lambda: verifier
    ov[get_object_store] = lambda: InMemoryObjectStore()
    ov[get_presign_ttl] = lambda: _m04.PRESIGN_TTL
    ov[get_knowledge_repository] = lambda: holder.current.repo
    ov[get_embedder] = lambda: holder.current.embedder
    ov[get_artifact_store] = lambda: holder.current.artifacts
    ov[get_search_limiter] = lambda: holder.current.limiter
    ov[get_search_settings] = lambda: SearchSettings()
    ov[get_graph_read_repository] = lambda: gholder.graph
    ov[get_graph_read_settings] = lambda: GraphReadSettings()
    return application


@pytest.fixture(scope="session")
def client(app: FastAPI) -> Iterator[TestClient]:  # type: ignore[no-redef]
    with TestClient(app) as test_client:
        yield test_client
    dispose_engines()


@pytest.fixture
def rig(  # type: ignore[no-redef]
    holder: Any, gholder: GraphHolder, admin_engine: Engine, app_engine: Engine
) -> Rig:
    current = Rig(admin_engine, app_engine, InMemoryKnowledgeRepository(), real=False)
    holder.current = current
    gholder.graph = FakeGraphRead()
    return current


@pytest.fixture
def real_rig(
    request: pytest.FixtureRequest,
    holder: Any,
    gholder: GraphHolder,
    admin_engine: Engine,
    app_engine: Engine,
    world: Any,
) -> Iterator[Rig]:
    rushdb = request.getfixturevalue("rushdb_settings")
    current = Rig(admin_engine, app_engine, RushDbKnowledgeRepository(rushdb), real=True)
    holder.current = current
    gholder.graph = FakeGraphRead()
    yield current
    for org in current.orgs | {world.org_id, world.foreign.org_id}:
        purge_tenant(rushdb.RUSHDB_URL, rushdb.RUSHDB_TOKEN.get_secret_value(), f"org:{org}")
