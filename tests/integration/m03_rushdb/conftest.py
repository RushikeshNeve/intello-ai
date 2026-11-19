"""Fixtures for the RushDB knowledge-repository tests (M03.03.01): REAL RushDB 3.0.10 + PostgreSQL.

* PostgreSQL: the M01 foundation fixtures (migrated to head) under Compose project
  ``ukos-m03-rdb`` (``POSTGRES_HOST_PORT=35983``, ``REDIS_HOST_PORT=36983``).
* RushDB: ``infrastructure/compose/rushdb.yml`` (image pinned ``rushdb/rushdb:3.0.10``) under its
  own project ``ukos-m03-rdb-rushdb`` on host port ``RUSHDB_HTTP_HOST_PORT`` (33311).
* Organizations are real rows; scopes are minted by ``validated_org_scope`` through a real session.

::

    RUSHDB_HTTP_HOST_PORT=33311 UKOS_TEST_COMPOSE_PROJECT=ukos-m03-rdb POSTGRES_HOST_PORT=35983 \\
        REDIS_HOST_PORT=36983 bash scripts/dev/linux-runner/run.sh apps/api \\
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m03_rushdb"

Set ``UKOS_TEST_KEEP_STACK=1`` to leave the stacks running (default: ``down -v``). The Linux runner
does not pass ``RUSHDB_*`` variables through; use ``UKOS_TEST_RUSHDB_PORT`` to change the port.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m03-rdb")
os.environ.setdefault("POSTGRES_HOST_PORT", "35983")
os.environ.setdefault("REDIS_HOST_PORT", "36983")
os.environ.setdefault("RUSHDB_HTTP_HOST_PORT", "33311")

REPO_ROOT = Path(__file__).resolve().parents[3]
API_DIR = REPO_ROOT / "apps" / "api"
_HERE = Path(__file__).resolve().parent
_FOUNDATION_DIR = REPO_ROOT / "tests" / "integration" / "m01_foundation"
for extra in (_HERE, _FOUNDATION_DIR, API_DIR):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def _load_foundation_fixtures() -> Any:
    # Imported under a unique name: several folders have a conftest.py.
    spec = importlib.util.spec_from_file_location(
        "m01_foundation_fixtures_rushdb", _FOUNDATION_DIR / "conftest.py"
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

from compose_support import (  # type: ignore[import-not-found,unused-ignore]
    ComposeProject,
    compose_file,
    wait_until,
)
from m01_seed import add_member, insert_org, insert_user  # type: ignore[import-not-found,unused-ignore]
from rdb_support import RecordingTransport, RushDbStack, purge_tenant
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.infrastructure.rushdb.memory import InMemoryKnowledgeRepository
from app.infrastructure.rushdb.repository import RushDbKnowledgeRepository
from app.modules.knowledge.ports import (
    KnowledgeRepository,
    OrgScope,
    validated_org_scope,
)

RUSHDB_PROJECT = os.environ.get("UKOS_TEST_RUSHDB_PROJECT", "ukos-m03-rdb-rushdb")
RUSHDB_PORT = os.environ.get("UKOS_TEST_RUSHDB_PORT", os.environ["RUSHDB_HTTP_HOST_PORT"])
# The Linux runner uses --network host, so published ports answer on 127.0.0.1.
RUSHDB_HOST = os.environ.get("RUSHDB_TEST_HOST", "127.0.0.1")


@pytest.fixture(scope="session")
def rushdb_stack() -> Iterator[RushDbStack]:
    project = ComposeProject(
        [compose_file("rushdb.yml")],
        project=RUSHDB_PROJECT,
        extra_env={"RUSHDB_HTTP_HOST_PORT": RUSHDB_PORT},
    )
    project.down(volumes=True)
    try:
        project.up("rushdb", wait=True)
        url = f"http://{RUSHDB_HOST}:{RUSHDB_PORT}"

        def ready() -> bool:
            return httpx.get(f"{url}/ready", timeout=3.0).status_code == 200

        wait_until(ready, timeout=60, description="RushDB /ready")
        yield RushDbStack(project, url, project.env["RUSHDB_TOKEN"])
    finally:
        if os.environ.get("UKOS_TEST_KEEP_STACK") != "1":
            project.down(volumes=True)


@pytest.fixture
def recording() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def rushdb_repo(
    rushdb_stack: RushDbStack, recording: RecordingTransport
) -> Iterator[RushDbKnowledgeRepository]:
    """The real adapter; every request it sends is recorded in ``recording``."""
    settings = rushdb_stack.settings()
    client = httpx.Client(
        base_url=settings.RUSHDB_URL, transport=recording.wrap(), timeout=httpx.Timeout(30.0)
    )
    repo = RushDbKnowledgeRepository(settings, client=client)
    yield repo
    client.close()


@pytest.fixture
def pg_session(admin_engine: Engine, migrated: None) -> Iterator[Session]:
    """A session that sees every organization (admin role); orgs are live rows."""
    session = Session(admin_engine)
    yield session
    session.close()


@pytest.fixture
def org_factory(admin_engine: Engine, migrated: None) -> Callable[[], uuid.UUID]:
    def make() -> uuid.UUID:
        with admin_engine.begin() as conn:  # an org needs an owner membership (deferred trigger)
            owner = insert_user(conn)
            org: uuid.UUID = insert_org(conn, created_by=owner)
            add_member(conn, org, owner, "owner")
            return org

    return make


@pytest.fixture
def minted_tenants(rushdb_stack: RushDbStack) -> Iterator[list[str]]:
    """Tenant strings created during a test; purged from RushDB afterwards."""
    tenants: list[str] = []
    yield tenants
    for tenant in tenants:
        purge_tenant(rushdb_stack.url, rushdb_stack.token, tenant)


@pytest.fixture
def scope_factory(
    pg_session: Session, org_factory: Callable[[], uuid.UUID], minted_tenants: list[str]
) -> Callable[[], OrgScope]:
    """Fresh live organizations; scopes come only from ``validated_org_scope`` (real PostgreSQL)."""

    def make() -> OrgScope:
        scope = validated_org_scope(pg_session, org_factory())
        minted_tenants.append(scope.tenant)
        return scope

    return make


@pytest.fixture(params=["rushdb", "memory"])
def any_repo(request: pytest.FixtureRequest) -> KnowledgeRepository:
    """Both implementations; ``rushdb`` starts the compose stack lazily."""
    if request.param == "memory":
        return InMemoryKnowledgeRepository()
    repo: KnowledgeRepository = request.getfixturevalue("rushdb_repo")
    return repo
