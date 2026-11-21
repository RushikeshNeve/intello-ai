"""Fixtures of the M12.07 style tests: real PostgreSQL + RLS through the real app,
and the in-memory knowledge index.

::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m12-styles POSTGRES_HOST_PORT=35971 REDIS_HOST_PORT=36971 \\
        bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m12_styles"
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m12-styles")
os.environ.setdefault("POSTGRES_HOST_PORT", "35971")
os.environ.setdefault("REDIS_HOST_PORT", "36971")

REPO_ROOT = Path(__file__).resolve().parents[3]
_HERE = Path(__file__).resolve().parent
_RET_DIR = REPO_ROOT / "tests" / "integration" / "m04_retrieval"
_FOUNDATION_DIR = REPO_ROOT / "tests" / "integration" / "m01_foundation"
_TENANCY_DIR = REPO_ROOT / "tests" / "integration" / "m01_tenancy"
_CATALOG_DIR = REPO_ROOT / "tests" / "integration" / "m02_catalog"
_M02_DIR = REPO_ROOT / "tests" / "integration" / "m02_foundation"
_M03_DIR = REPO_ROOT / "tests" / "integration" / "m03_foundation"
_INFRA_DIR = REPO_ROOT / "tests" / "infrastructure"
for extra in (
    _HERE,
    _RET_DIR,
    _FOUNDATION_DIR,
    _TENANCY_DIR,
    _CATALOG_DIR,
    _M02_DIR,
    _M03_DIR,
    _INFRA_DIR,
):
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
from compose_support import ComposeProject
from fastapi.testclient import TestClient
from pydantic import SecretStr
from ret_support import Rig, RigHolder
from sqlalchemy import Engine
from sqlalchemy.engine import URL
from tenancy_support import Api, FakeVerifier

from app.core.config import Settings
from app.core.db import dispose_engines
from app.core.security import get_token_verifier
from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.infrastructure.rushdb.memory import InMemoryKnowledgeRepository
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


@pytest.fixture
def rig(holder: RigHolder, admin_engine: Engine, app_engine: Engine) -> Rig:
    """The in-memory knowledge index with a fake embedder and artifact store."""
    current = Rig(admin_engine, app_engine, InMemoryKnowledgeRepository(), real=False)
    holder.current = current
    return current
