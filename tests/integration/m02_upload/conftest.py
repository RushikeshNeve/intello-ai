"""Fixtures for the upload API tests (M02.01.03, M02.01.04).

Real PostgreSQL + RLS and a fake object store.

* PostgreSQL: the M01 foundation fixtures (``tests/integration/m01_foundation/conftest.py``) under
  Compose project ``ukos-m02-upload`` (``POSTGRES_HOST_PORT=35941``, ``REDIS_HOST_PORT=36941``).
* The app is ``create_app`` on the app role (``ukos_app``, NOBYPASSRLS) with the real provisioner;
  ``get_token_verifier`` is a :class:`FakeVerifier` and ``get_object_store`` an
  :class:`InMemoryObjectStore` (a fresh one per test, see ``store``).
* ``test_upload_e2e_rustfs.py`` additionally starts the real object store (``objects.yml``, same
  project, ``OBJECT_STORE_HOST_PORT=39311``) and uses the real ``S3ObjectStore``.

::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m02-upload POSTGRES_HOST_PORT=35941 REDIS_HOST_PORT=36941 \\
    OBJECT_STORE_HOST_PORT=39311 bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m02_upload"

Set ``UKOS_TEST_KEEP_STACK=1`` to leave the stacks running (default: ``down -v``).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m02-upload")
os.environ.setdefault("POSTGRES_HOST_PORT", "35941")
os.environ.setdefault("REDIS_HOST_PORT", "36941")
os.environ.setdefault("OBJECT_STORE_HOST_PORT", "39311")

REPO_ROOT = Path(__file__).resolve().parents[3]
_HERE = Path(__file__).resolve().parent
_FOUNDATION_DIR = REPO_ROOT / "tests" / "integration" / "m01_foundation"
_TENANCY_DIR = REPO_ROOT / "tests" / "integration" / "m01_tenancy"
for extra in (_HERE, _FOUNDATION_DIR, _TENANCY_DIR, REPO_ROOT / "tests" / "infrastructure"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def _load_foundation_fixtures() -> Any:
    # Imported under a unique name: both files are called conftest.py.
    spec = importlib.util.spec_from_file_location(
        "m01_foundation_fixtures_m02_upload", _FOUNDATION_DIR / "conftest.py"
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

from compose_support import ComposeProject
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.engine import URL
from tenancy_support import FakeVerifier
from upload_support import Scene, StoreHolder, UploadApi

from app.core.config import Settings, get_settings
from app.core.db import dispose_engines
from app.core.security import get_token_verifier
from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.main import create_app
from app.modules.storage.deps import get_object_store


@pytest.fixture(scope="session")
def app_db_url(stack: ComposeProject, migrated: None) -> URL:
    url: URL = _db_url(stack, app=True)
    return url


@pytest.fixture(scope="session")
def verifier() -> FakeVerifier:
    return FakeVerifier()


@pytest.fixture(scope="session")
def holder() -> StoreHolder:
    return StoreHolder(InMemoryObjectStore())


@pytest.fixture(scope="session")
def base_settings(app_db_url: URL) -> Settings:
    return Settings(DATABASE_URL=SecretStr(app_db_url.render_as_string(hide_password=False)))


@pytest.fixture(scope="session")
def client(
    base_settings: Settings, verifier: FakeVerifier, holder: StoreHolder
) -> Iterator[TestClient]:
    app = create_app(base_settings)
    app.dependency_overrides[get_token_verifier] = lambda: verifier
    app.dependency_overrides[get_object_store] = lambda: holder.store
    with TestClient(app) as test_client:
        yield test_client
    dispose_engines()


@pytest.fixture
def store(holder: StoreHolder) -> InMemoryObjectStore:
    """A fresh in-memory object store for this test (the app uses it)."""
    fresh = InMemoryObjectStore()
    holder.store = fresh
    return fresh


@pytest.fixture
def api(
    client: TestClient, verifier: FakeVerifier, admin_engine: Engine, store: InMemoryObjectStore
) -> UploadApi:
    return UploadApi(client, verifier, admin_engine, store)


@pytest.fixture
def scene(api: UploadApi) -> Scene:
    return api.scene()


@pytest.fixture
def limits(client: TestClient, base_settings: Settings) -> Iterator[Callable[..., None]]:
    """``limits(UPLOAD_MAX_BYTES=10, ...)``: override quota settings until the test ends."""
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]

    def apply(**values: Any) -> None:
        overrides[get_settings] = lambda: base_settings.model_copy(update=values)

    yield apply
    overrides[get_settings] = lambda: base_settings
