"""Fixtures of the M14.08 envelope-encryption tests: the M06 connector harness (real PostgreSQL, the
real API in-process, the sync as the worker role, fake GitHub) with a master key FILE configured,
under their own compose project and ports::

    UKOS_TEST_COMPOSE_PROJECT=ukos-m14-crypto POSTGRES_HOST_PORT=35814 REDIS_HOST_PORT=36814 \\
        bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m14_crypto"
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m14-crypto")
os.environ.setdefault("POSTGRES_HOST_PORT", "35814")
os.environ.setdefault("REDIS_HOST_PORT", "36814")

_M06 = Path(__file__).resolve().parents[1] / "m06_connectors"
for _extra in (Path(__file__).resolve().parent, _M06):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))
_spec = importlib.util.spec_from_file_location("m06_fixtures_for_m14", _M06 / "conftest.py")
assert _spec is not None
assert _spec.loader is not None
_m06 = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _m06
_spec.loader.exec_module(_m06)

from compose_support import ComposeProject
from m03_support import make_url
from m14_support import KEK1
from pydantic import SecretStr

from app.core.config import Settings

for _name in (
    "stack",
    "admin_engine",
    "migrated",
    "worker_engine",
    "verifier",
    "app",
    "client",
    "api",
    "world",
    "fake_gh",
    "deps",
):
    globals()[_name] = getattr(_m06, _name)

LEGACY_KEY = _m06.TEST_KEY  # the M06 single connector key (id k1)


@pytest.fixture(autouse=True)
def _seed_repo(fake_gh: object) -> None:
    fake_gh.commit({"README.md": b"# readme\n"})  # type: ignore[attr-defined]


@pytest.fixture(scope="session")
def kek_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("kek") / "master.key"
    path.write_text(KEK1 + "\n", encoding="utf-8")
    return path


@pytest.fixture(scope="session")
def base_settings(stack: ComposeProject, migrated: None, kek_file: Path) -> Settings:
    url = make_url(stack, "app").render_as_string(hide_password=False)
    return Settings(
        DATABASE_URL=SecretStr(url),
        CONNECTOR_ENCRYPTION_KEY=SecretStr(LEGACY_KEY),
        MASTER_KEY_FILE=str(kek_file),
        MASTER_KEY_ID="kek1",
    )
