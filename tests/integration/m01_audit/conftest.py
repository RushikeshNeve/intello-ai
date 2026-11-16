"""DB-backed fixtures for the audit trail (M01.03.04).

Reuses the M01 foundation fixtures (``stack``, ``admin_engine``, ``app_engine``, ``migrated``) by
loading ``../m01_foundation/conftest.py`` under a private module name, and its seed helpers. The
default Compose project is ``ukos-m01-audit``; run in isolation::

    cd apps/api
    UKOS_TEST_COMPOSE_PROJECT=ukos-m01-audit POSTGRES_HOST_PORT=25542 REDIS_HOST_PORT=26542 \\
        uv run --no-sync python -m pytest -q ../../tests/integration/m01_audit

Don't collect this directory in the same pytest run as ``m01_foundation``: both would manage the
same session stack.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

FOUNDATION_DIR = Path(__file__).resolve().parents[1] / "m01_foundation"
if str(FOUNDATION_DIR) not in sys.path:
    sys.path.insert(0, str(FOUNDATION_DIR))
os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m01-audit")

_spec = importlib.util.spec_from_file_location(
    "m01_foundation_fixtures", FOUNDATION_DIR / "conftest.py"
)
if _spec is None or _spec.loader is None:
    raise ImportError(f"cannot load {FOUNDATION_DIR / 'conftest.py'}")
_foundation = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _foundation
_spec.loader.exec_module(_foundation)

stack = _foundation.stack
admin_engine = _foundation.admin_engine
app_engine = _foundation.app_engine
alembic_config = _foundation.alembic_config
migrated = _foundation.migrated
