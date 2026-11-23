"""Fixtures of the M14.07 abuse/canary tests: real PostgreSQL + RLS through the real app.

::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m14-abuse POSTGRES_HOST_PORT=38071 REDIS_HOST_PORT=38072 \\
        bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m14_abuse"
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m14-abuse")
os.environ.setdefault("POSTGRES_HOST_PORT", "38071")
os.environ.setdefault("REDIS_HOST_PORT", "38072")

REPO_ROOT = Path(__file__).resolve().parents[3]
_FOUNDATION_DIR = REPO_ROOT / "tests" / "integration" / "m01_foundation"
for extra in (
    Path(__file__).resolve().parent,
    _FOUNDATION_DIR,
    REPO_ROOT / "tests" / "integration" / "m01_tenancy",
    REPO_ROOT / "tests" / "integration" / "m01_identity",
    REPO_ROOT / "tests" / "infrastructure",
):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def _load_foundation_fixtures() -> Any:
    spec = importlib.util.spec_from_file_location(
        "m01_foundation_fixtures_m14_abuse", _FOUNDATION_DIR / "conftest.py"
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
from pydantic import SecretStr  # noqa: E402
from sqlalchemy import Engine  # noqa: E402
from sqlalchemy.engine import URL  # noqa: E402
from tenancy_support import Api, FakeVerifier  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.db import dispose_engines  # noqa: E402
from app.core.security import get_token_verifier  # noqa: E402
from app.main import create_app  # noqa: E402
from app.modules.firewall import abuse  # noqa: E402


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


@pytest.fixture(autouse=True)
def _fresh_counters() -> Iterator[None]:
    abuse.set_counters(abuse.Counters(None))  # in-process window, no Redis
    yield
    abuse.set_counters(None)
