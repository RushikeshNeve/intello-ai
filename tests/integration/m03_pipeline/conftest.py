"""Fixtures of the M03 pipeline tests: real PostgreSQL (worker role, RLS), in-memory stores by
default, REAL RushDB 3.0.10 and REAL object store (RustFS) for the tests that ask for them.

::

    RUSHDB_HTTP_HOST_PORT=33321 UKOS_TEST_COMPOSE_PROJECT=ukos-m03-pipe POSTGRES_HOST_PORT=35992 \\
        REDIS_HOST_PORT=36992 bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m03_pipeline"

Compose projects: ``ukos-m03-pipe`` (postgres), ``ukos-m03-pipe-rushdb``, ``ukos-m03-pipe-objs``
(object store on ``OBJECT_STORE_HOST_PORT``, default 39321). ``UKOS_TEST_KEEP_STACK=1`` keeps them.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m03-pipe")
os.environ.setdefault("POSTGRES_HOST_PORT", "35992")
os.environ.setdefault("REDIS_HOST_PORT", "36992")
os.environ.setdefault("RUSHDB_HTTP_HOST_PORT", "33321")
os.environ.setdefault("OBJECT_STORE_HOST_PORT", "39321")

REPO_ROOT = Path(__file__).resolve().parents[3]
API_DIR = REPO_ROOT / "apps" / "api"
_HERE = Path(__file__).resolve().parent
_FOUNDATION = REPO_ROOT / "tests" / "integration" / "m03_foundation"
for extra in (_HERE, _FOUNDATION, API_DIR, REPO_ROOT / "tests" / "integration" / "m03_rushdb"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def _load_foundation() -> Any:
    spec = importlib.util.spec_from_file_location(
        "m03_foundation_fixtures_pipeline", _FOUNDATION / "conftest.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_found = _load_foundation()
stack = _found.stack
admin_engine = _found.admin_engine
alembic_config = _found.alembic_config
migrated = _found.migrated
worker_engine = _found.worker_engine
app_engine = _found.app_engine

from compose_support import (
    ComposeProject,
    compose_file,
    wait_until,
)
from pipe_support import Env
from pydantic import SecretStr
from rdb_support import purge_tenant
from sqlalchemy import Engine

from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.infrastructure.object_storage.s3 import S3ObjectStore
from app.infrastructure.object_storage.settings import ObjectStoreSettings
from app.infrastructure.rushdb.memory import InMemoryKnowledgeRepository
from app.infrastructure.rushdb.repository import RushDbKnowledgeRepository
from app.infrastructure.rushdb.settings import RushDbSettings

RUSHDB_PORT = os.environ["RUSHDB_HTTP_HOST_PORT"]
OBJECT_PORT = os.environ["OBJECT_STORE_HOST_PORT"]
BASE = os.environ["UKOS_TEST_COMPOSE_PROJECT"]


@pytest.fixture(scope="session")
def rushdb_settings() -> Iterator[RushDbSettings]:
    project = ComposeProject(
        [compose_file("rushdb.yml")],
        project=BASE + "-rushdb",
        extra_env={"RUSHDB_HTTP_HOST_PORT": RUSHDB_PORT},
    )
    project.down(volumes=True)
    try:
        project.up("rushdb", wait=True)
        url = f"http://127.0.0.1:{RUSHDB_PORT}"
        wait_until(
            lambda: httpx.get(f"{url}/ready", timeout=3.0).status_code == 200,
            timeout=60,
            description="RushDB /ready",
        )
        yield RushDbSettings(RUSHDB_URL=url, RUSHDB_TOKEN=SecretStr(project.env["RUSHDB_TOKEN"]))
    finally:
        if os.environ.get("UKOS_TEST_KEEP_STACK") != "1":
            project.down(volumes=True)


@pytest.fixture(scope="session")
def real_stores() -> Iterator[tuple[S3ObjectStore, S3ObjectStore]]:
    """(uploads bucket, artifacts bucket) on a real RustFS."""
    url = f"http://127.0.0.1:{OBJECT_PORT}"
    project = ComposeProject(
        [compose_file("objects.yml")],
        project=BASE + "-objs",
        extra_env={"OBJECT_STORE_HOST_PORT": OBJECT_PORT, "S3_PUBLIC_ENDPOINT_URL": url},
    )
    project.down(volumes=True)
    try:
        project.up("objectstore", wait=True)
        project.run("run", "--rm", "objectstore-init")
        env = project.env
        settings = ObjectStoreSettings(
            S3_ENDPOINT_URL=url,
            S3_PUBLIC_ENDPOINT_URL=url,
            S3_REGION=env["S3_REGION"],
            S3_ACCESS_KEY_ID=SecretStr(env["S3_ACCESS_KEY_ID"]),
            S3_SECRET_ACCESS_KEY=SecretStr(env["S3_SECRET_ACCESS_KEY"]),
            S3_BUCKET_ORIGINALS=env["S3_BUCKET_ORIGINALS"],
            S3_BUCKET_ARTIFACTS=env["S3_BUCKET_ARTIFACTS"],
        )
        uploads = S3ObjectStore(settings, bucket=settings.S3_BUCKET_ORIGINALS)
        arts = S3ObjectStore(settings, bucket=settings.S3_BUCKET_ARTIFACTS)
        wait_until(lambda: uploads.head_object("probe") is None, timeout=30, description="S3")
        yield uploads, arts
    finally:
        if os.environ.get("UKOS_TEST_KEEP_STACK") != "1":
            project.down(volumes=True)


@pytest.fixture
def make_env(
    request: pytest.FixtureRequest, admin_engine: Engine, worker_engine: Engine, migrated: None
) -> Iterator[Callable[..., Env]]:
    """Factory ``make_env(real_index=False, real_store=False)``; cleans real tenants afterwards."""
    created: list[Env] = []

    def make(*, real_index: bool = False, real_store: bool = False) -> Env:
        if real_index:
            settings: RushDbSettings = request.getfixturevalue("rushdb_settings")
            repo: Any = RushDbKnowledgeRepository(settings)
        else:
            repo = InMemoryKnowledgeRepository()
        if real_store:
            uploads, arts = request.getfixturevalue("real_stores")
        else:
            uploads, arts = InMemoryObjectStore(), InMemoryObjectStore()
        env = Env(
            admin_engine,
            worker_engine,
            repo,
            uploads,
            arts,
            rushdb=settings if real_index else None,
        )
        created.append(env)
        return env

    yield make
    for env in created:
        env.cleanup(purge_tenant)


@pytest.fixture(params=["memory", "real"])
def env(request: pytest.FixtureRequest, make_env: Callable[..., Env]) -> Env:
    """Both index implementations (the real one starts RushDB lazily)."""
    return make_env(real_index=request.param == "real")
