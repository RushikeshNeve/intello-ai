"""Fixtures for the document catalog API tests (M02.02.02 - M02.02.04): real PostgreSQL, real RLS.

* PostgreSQL: the M01 foundation fixtures (loaded below; they run every migration to ``head``)
  under project ``ukos-m02-catalog`` (``POSTGRES_HOST_PORT=35942``, ``REDIS_HOST_PORT=36942``).
* The app is ``create_app`` on the app role (``ukos_app``, NOBYPASSRLS) with the real provisioner;
  ``get_token_verifier`` is a fake and the object store is an ``InMemoryObjectStore``.
* Documents and versions are **seeded as admin** (``catalog_support``); the upload endpoints are
  not used.

::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m02-catalog POSTGRES_HOST_PORT=35942 REDIS_HOST_PORT=36942 \\
        bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m02_catalog"

Set ``UKOS_TEST_KEEP_STACK=1`` to leave the stack running (default: ``down -v``).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m02-catalog")
os.environ.setdefault("POSTGRES_HOST_PORT", "35942")
os.environ.setdefault("REDIS_HOST_PORT", "36942")

REPO_ROOT = Path(__file__).resolve().parents[3]
_HERE = Path(__file__).resolve().parent
_FOUNDATION_DIR = REPO_ROOT / "tests" / "integration" / "m01_foundation"
_TENANCY_DIR = REPO_ROOT / "tests" / "integration" / "m01_tenancy"
for extra in (_HERE, _FOUNDATION_DIR, _TENANCY_DIR):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def _load_foundation_fixtures() -> Any:
    # Imported under a unique name: both files are called conftest.py.
    spec = importlib.util.spec_from_file_location(
        "m01_foundation_fixtures_catalog", _FOUNDATION_DIR / "conftest.py"
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
from sqlalchemy import Engine
from sqlalchemy.engine import URL
from tenancy_support import Api, FakeVerifier

from app.core.config import Settings
from app.core.db import dispose_engines
from app.core.security import get_token_verifier
from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.main import create_app
from app.modules.documents.versions import get_presign_ttl
from app.modules.storage.deps import get_object_store


@pytest.fixture(scope="session")
def app_db_url(stack: ComposeProject, migrated: None) -> URL:
    url: URL = _db_url(stack, app=True)
    return url


@pytest.fixture(scope="session")
def verifier() -> FakeVerifier:
    return FakeVerifier()


@pytest.fixture(scope="session")
def store() -> InMemoryObjectStore:
    return InMemoryObjectStore()


@pytest.fixture(scope="session")
def client(
    app_db_url: URL, verifier: FakeVerifier, store: InMemoryObjectStore
) -> Iterator[TestClient]:
    settings = Settings(DATABASE_URL=SecretStr(app_db_url.render_as_string(hide_password=False)))
    app = create_app(settings)
    app.dependency_overrides[get_token_verifier] = lambda: verifier
    app.dependency_overrides[get_object_store] = lambda: store
    app.dependency_overrides[get_presign_ttl] = lambda: PRESIGN_TTL
    with TestClient(app) as test_client:
        yield test_client
    dispose_engines()


@pytest.fixture
def api(client: TestClient, verifier: FakeVerifier, admin_engine: Engine) -> Api:
    return Api(client, verifier, admin_engine)


@pytest.fixture
def world(api: Api) -> World:
    """An org with one user per role, a workspace and a second (foreign) org."""
    return build_world(api)
