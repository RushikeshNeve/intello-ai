"""Fixtures for the M01 identity integration tests (real Dex, real PostgreSQL).

* Dex: ``infrastructure/compose/identity.yml`` under project ``ukos-m01-identity`` on host port
  28381 (``OIDC_ISSUER_URL=http://127.0.0.1:28381/dex``), plus a short-TTL Dex under
  ``ukos-m01-identity-exp`` on 28382 for real expired tokens.
* PostgreSQL: the M01 foundation fixtures (``tests/integration/m01_foundation/conftest.py``,
  loaded below) under project ``ukos-m01-identity-db`` with ``POSTGRES_HOST_PORT=25541`` and
  ``REDIS_HOST_PORT=26541``.

Every default can be overridden from the environment::

    cd apps/api
    uv run --no-sync python -m pytest -q ../../tests/integration/m01_identity

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

# Isolated ports/projects for this suite. Set before the foundation fixtures and compose_support
# read the environment.
os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m01-identity-db")
os.environ.setdefault("POSTGRES_HOST_PORT", "25541")
os.environ.setdefault("REDIS_HOST_PORT", "26541")

REPO_ROOT = Path(__file__).resolve().parents[3]
_FOUNDATION_DIR = REPO_ROOT / "tests" / "integration" / "m01_foundation"
if str(_FOUNDATION_DIR) not in sys.path:
    sys.path.insert(0, str(_FOUNDATION_DIR))


def _load_foundation_fixtures() -> Any:
    # Imported under a unique name: both files are called conftest.py.
    spec = importlib.util.spec_from_file_location(
        "m01_foundation_fixtures", _FOUNDATION_DIR / "conftest.py"
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
from m01_identity_support import (  # noqa: E402
    DEX_EXP_PORT,
    DEX_EXP_PROJECT,
    DEX_EXP_TTL_SECONDS,
    DEX_PORT,
    DEX_PROJECT,
    Dex,
    start_dex,
)
from sqlalchemy.engine import URL  # noqa: E402


@pytest.fixture(scope="session")
def dex() -> Iterator[Dex]:
    yield from start_dex(DEX_PROJECT, DEX_PORT)


@pytest.fixture(scope="session")
def dex_short_ttl() -> Iterator[Dex]:
    yield from start_dex(DEX_EXP_PROJECT, DEX_EXP_PORT, OIDC_ID_TOKEN_TTL=f"{DEX_EXP_TTL_SECONDS}s")


@pytest.fixture(scope="session")
def app_db_url(stack: ComposeProject, migrated: None) -> URL:
    """SQLAlchemy URL for the app role (``ukos_app``, NOBYPASSRLS) of the test database."""
    url: URL = _db_url(stack, app=True)
    return url
