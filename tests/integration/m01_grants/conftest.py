"""DB-backed fixtures for document grants (M01.03.03).

Reuses the M01 foundation fixtures (``stack``, ``admin_engine``, ``app_engine``, ``migrated``) under
its own Compose project, so it can run beside other suites::

    cd apps/api
    UKOS_TEST_COMPOSE_PROJECT=ukos-m01-grants POSTGRES_HOST_PORT=25543 REDIS_HOST_PORT=26543 \\
        uv run --no-sync python -m pytest -q ../../tests/integration/m01_grants

The project defaults to ``ukos-m01-grants``; the stack is removed with ``down -v`` afterwards unless
``UKOS_TEST_KEEP_STACK=1``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FOUNDATION_DIR = HERE.parent / "m01_foundation"
for extra in (HERE, FOUNDATION_DIR):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

# Must be set before the foundation conftest is executed: it reads the project name at import.
os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m01-grants")

_spec = importlib.util.spec_from_file_location(
    "m01_foundation_fixtures", FOUNDATION_DIR / "conftest.py"
)
if _spec is None or _spec.loader is None:
    raise ImportError(f"cannot load {FOUNDATION_DIR / 'conftest.py'}")
_foundation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_foundation)

stack = _foundation.stack
admin_engine = _foundation.admin_engine
app_engine = _foundation.app_engine
alembic_config = _foundation.alembic_config
migrated = _foundation.migrated

from grants_seed import GrantTenant, seed_grant_tenant  # noqa: E402
from sqlalchemy import Engine  # noqa: E402


@pytest.fixture
def tenant(admin_engine: Engine, migrated: None) -> GrantTenant:
    return seed_grant_tenant(admin_engine)


@pytest.fixture
def other_tenant(admin_engine: Engine, migrated: None) -> GrantTenant:
    return seed_grant_tenant(admin_engine)
