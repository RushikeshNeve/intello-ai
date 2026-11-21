"""Fixtures for the M11.03 embedding-cache RLS test: the M01 foundation stack (real PostgreSQL),
own Compose project and ports::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m11-cache POSTGRES_HOST_PORT=25611 REDIS_HOST_PORT=26611 \\
        python -m pytest -q ../../tests/integration/m11_cache
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m11-cache")
os.environ.setdefault("POSTGRES_HOST_PORT", "25611")
os.environ.setdefault("REDIS_HOST_PORT", "26611")

_FOUNDATION_DIR = Path(__file__).resolve().parents[1] / "m01_foundation"
for extra in (_FOUNDATION_DIR,):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def _load_foundation_fixtures() -> Any:
    spec = importlib.util.spec_from_file_location(
        "m01_foundation_fixtures_m11_cache", _FOUNDATION_DIR / "conftest.py"
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
