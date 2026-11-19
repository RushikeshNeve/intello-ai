"""Fixtures of the M05 graph read-API integration tests (real PostgreSQL + RLS, real app).

The graph store is the in-memory fake; the chunk text comes from the real artifact path.
Reuses the M04 retrieval fixtures (stack, world, api, ...) by loading that conftest::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m05-b-api POSTGRES_HOST_PORT=35971 REDIS_HOST_PORT=36971 \\
        bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m05_graph_api"
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m05-b-api")
os.environ.setdefault("POSTGRES_HOST_PORT", "35971")
os.environ.setdefault("REDIS_HOST_PORT", "36971")
os.environ.setdefault("RUSHDB_HTTP_HOST_PORT", "33971")

_M04 = Path(__file__).resolve().parents[1] / "m04_retrieval"
_spec = importlib.util.spec_from_file_location(
    "m04_retrieval_fixtures_for_m05", _M04 / "conftest.py"
)
assert _spec is not None
assert _spec.loader is not None
_m04 = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _m04
_spec.loader.exec_module(_m04)

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from ret_support import Rig
from sqlalchemy import Engine

from app.core.config import Settings
from app.core.db import dispose_engines
from app.core.security import get_token_verifier
from app.infrastructure.rushdb.memory import InMemoryKnowledgeRepository
from app.main import create_app
from app.modules.documents.versions import get_presign_ttl
from app.modules.graph.fake_read import FakeGraphRead
from app.modules.graph.read_settings import GraphReadSettings, get_graph_read_settings
from app.modules.graph.wiring import get_graph_read_repository
from app.modules.retrieval.wiring import get_artifact_store
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
):
    globals()[_name] = getattr(_m04, _name)


class GraphHolder:
    graph: FakeGraphRead
    rig: Rig


@pytest.fixture(scope="session")
def gholder() -> GraphHolder:
    return GraphHolder()


@pytest.fixture(scope="session")
def client(app_db_url: Any, verifier: Any, gholder: GraphHolder) -> Iterator[TestClient]:  # type: ignore[no-redef]
    settings = Settings(DATABASE_URL=SecretStr(app_db_url.render_as_string(hide_password=False)))
    app = create_app(settings)
    app.dependency_overrides[get_token_verifier] = lambda: verifier
    app.dependency_overrides[get_object_store] = lambda: _m04.InMemoryObjectStore()
    app.dependency_overrides[get_presign_ttl] = lambda: _m04.PRESIGN_TTL
    app.dependency_overrides[get_graph_read_repository] = lambda: gholder.graph
    app.dependency_overrides[get_artifact_store] = lambda: gholder.rig.artifacts
    app.dependency_overrides[get_graph_read_settings] = lambda: GraphReadSettings()
    with TestClient(app) as test_client:
        yield test_client
    dispose_engines()


@pytest.fixture
def rig(gholder: GraphHolder, admin_engine: Engine, app_engine: Engine) -> Rig:  # type: ignore[no-redef]
    current = Rig(admin_engine, app_engine, InMemoryKnowledgeRepository(), real=False)
    gholder.rig = current
    return current
