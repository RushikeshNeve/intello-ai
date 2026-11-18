"""Fixtures for the tenancy REST API tests (M01.02.02, M01.02.04): real PostgreSQL, real RLS.

* PostgreSQL: the M01 foundation fixtures (``tests/integration/m01_foundation/conftest.py``,
  loaded below) under project ``ukos-m01-tenancy`` with ``POSTGRES_HOST_PORT=25551`` and
  ``REDIS_HOST_PORT=26551``.
* The app is ``create_app`` on the app role (``ukos_app``, NOBYPASSRLS) with the **real**
  provisioner; only ``get_token_verifier`` is overridden by :class:`FakeVerifier`, which maps test
  tokens to ``VerifiedClaims``.
* ``test_dex_e2e.py`` additionally starts a real Dex (``identity.yml``) under project
  ``ukos-m01-tenancy-dex`` on host port 28391.

::

    cd apps/api
    uv run --no-sync python -m pytest -q ../../tests/integration/m01_tenancy

Set ``UKOS_TEST_KEEP_STACK=1`` to leave the stacks running (default: ``down -v``).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m01-tenancy")
os.environ.setdefault("POSTGRES_HOST_PORT", "25551")
os.environ.setdefault("REDIS_HOST_PORT", "26551")

REPO_ROOT = Path(__file__).resolve().parents[3]
_HERE = Path(__file__).resolve().parent
_FOUNDATION_DIR = REPO_ROOT / "tests" / "integration" / "m01_foundation"
_IDENTITY_DIR = REPO_ROOT / "tests" / "integration" / "m01_identity"
for extra in (_HERE, _FOUNDATION_DIR, _IDENTITY_DIR):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def _load_foundation_fixtures() -> Any:
    # Imported under a unique name: both files are called conftest.py.
    spec = importlib.util.spec_from_file_location(
        "m01_foundation_fixtures_tenancy", _FOUNDATION_DIR / "conftest.py"
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

from compose_support import ComposeProject  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from m01_identity_support import Dex, start_dex  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from sqlalchemy import Engine  # noqa: E402
from sqlalchemy.engine import URL  # noqa: E402
from tenancy_support import Api, FakeVerifier  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.db import dispose_engines  # noqa: E402
from app.core.security import get_token_verifier  # noqa: E402
from app.main import create_app  # noqa: E402

DEX_PROJECT = os.environ.get("UKOS_TEST_TENANCY_DEX_PROJECT", "ukos-m01-tenancy-dex")
DEX_PORT = int(os.environ.get("UKOS_TEST_TENANCY_DEX_PORT", "28391"))


@pytest.fixture(scope="session")
def app_db_url(stack: ComposeProject, migrated: None) -> URL:
    url: URL = _db_url(stack, app=True)
    return url


@pytest.fixture(scope="session")
def verifier() -> FakeVerifier:
    return FakeVerifier()


@pytest.fixture(scope="session")
def client(app_db_url: URL, verifier: FakeVerifier) -> Iterator[TestClient]:
    settings = Settings(DATABASE_URL=SecretStr(app_db_url.render_as_string(hide_password=False)))
    app = create_app(settings)
    app.dependency_overrides[get_token_verifier] = lambda: verifier
    with TestClient(app) as test_client:
        yield test_client
    dispose_engines()


@pytest.fixture
def api(client: TestClient, verifier: FakeVerifier, admin_engine: Engine) -> Api:
    return Api(client, verifier, admin_engine)


@pytest.fixture(scope="session")
def dex() -> Iterator[Dex]:
    yield from start_dex(DEX_PROJECT, DEX_PORT)
