"""Fixtures of the M12.05 re-index tests: reuse the M03 pipeline harness (real PostgreSQL, in-memory
index and object stores) under their own compose project and ports::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m12-reindex POSTGRES_HOST_PORT=35712 REDIS_HOST_PORT=36712 \\
        bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m12_reindex"
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m12-reindex")
os.environ.setdefault("POSTGRES_HOST_PORT", "35712")
os.environ.setdefault("REDIS_HOST_PORT", "36712")

_PIPE = Path(__file__).resolve().parents[1] / "m03_pipeline" / "conftest.py"
_spec = importlib.util.spec_from_file_location("m03_pipeline_fixtures_for_m12", _PIPE)
assert _spec is not None
assert _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

for _name in (
    "stack",
    "admin_engine",
    "alembic_config",
    "migrated",
    "worker_engine",
    "app_engine",
    "make_env",
):
    globals()[_name] = getattr(_module, _name)
