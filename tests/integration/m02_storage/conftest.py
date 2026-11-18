"""Fixtures for the object-store port contract suite (M02.01.01 / M02.01.02).

* The real store is RustFS from ``infrastructure/compose/objects.yml`` under Compose project
  ``ukos-m02-storage`` with host port 39301 (``OBJECT_STORE_HOST_PORT``). The fixture starts the
  store, runs ``objectstore-init`` to completion (buckets + CORS) and tears everything down with
  ``down -v`` (set ``UKOS_TEST_KEEP_STACK=1`` to keep it).
* ``store`` is parametrized over ``S3ObjectStore`` and ``InMemoryObjectStore``; ``browser`` plays
  the browser against whichever one is under test.

::

    scripts/dev/linux-runner/run.sh apps/api \
      "python -m pytest -q -p no:cacheprovider ../../tests/integration/m02_storage"
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

REPO_ROOT = Path(__file__).resolve().parents[3]
for extra in (
    REPO_ROOT / "apps" / "api",
    REPO_ROOT / "tests" / "infrastructure",
    Path(__file__).parent,
):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from compose_support import (  # type: ignore[import-not-found,unused-ignore]
    ComposeProject,
    compose_file,
    require_docker,
    wait_until,
)
from storage_support import Browser, Stack

from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.infrastructure.object_storage.s3 import S3ObjectStore
from app.infrastructure.object_storage.settings import ObjectStoreSettings
from app.modules.storage.keys import original_key
from app.modules.storage.ports import ObjectStore

PROJECT = os.environ.get("UKOS_TEST_STORAGE_PROJECT", "ukos-m02-storage")
HOST_PORT = os.environ.get("UKOS_TEST_STORAGE_PORT", "39301")
PUBLIC_URL = f"http://127.0.0.1:{HOST_PORT}"


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    require_docker()
    project = ComposeProject(
        [compose_file("objects.yml")],
        project=PROJECT,
        extra_env={"OBJECT_STORE_HOST_PORT": HOST_PORT, "S3_PUBLIC_ENDPOINT_URL": PUBLIC_URL},
    )
    project.down(volumes=True)
    try:
        project.up("objectstore", wait=True)
        # One-shot: creates both private buckets and applies the CORS rules from API_CORS_ORIGINS.
        project.run("run", "--rm", "objectstore-init")
        env = project.env
        settings = ObjectStoreSettings(
            S3_ENDPOINT_URL=PUBLIC_URL,  # the tests run on the host network, not in the compose one
            S3_PUBLIC_ENDPOINT_URL=PUBLIC_URL,
            S3_REGION=env["S3_REGION"],
            S3_ACCESS_KEY_ID=SecretStr(env["S3_ACCESS_KEY_ID"]),
            S3_SECRET_ACCESS_KEY=SecretStr(env["S3_SECRET_ACCESS_KEY"]),
            S3_BUCKET_ORIGINALS=env["S3_BUCKET_ORIGINALS"],
            S3_BUCKET_ARTIFACTS=env["S3_BUCKET_ARTIFACTS"],
        )
        origins = [o.strip() for o in env["API_CORS_ORIGINS"].split(",") if o.strip()]

        def ready() -> bool:
            return S3ObjectStore(settings).head_object("probe") is None

        wait_until(ready, timeout=30, description="object store S3 endpoint")
        yield Stack(project, settings, origins[0])
    finally:
        if os.environ.get("UKOS_TEST_KEEP_STACK") != "1":
            project.down(volumes=True)


@pytest.fixture(scope="session")
def http() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        yield client


@pytest.fixture(params=["s3", "memory"])
def store(request: pytest.FixtureRequest) -> Iterator[ObjectStore]:
    """The port under test. ``s3`` starts the compose stack lazily."""
    if request.param == "memory":
        yield InMemoryObjectStore()
        return
    stack: Stack = request.getfixturevalue("stack")
    yield S3ObjectStore(stack.settings)


@pytest.fixture
def s3_only(store: ObjectStore) -> S3ObjectStore:
    if not isinstance(store, S3ObjectStore):
        pytest.skip("needs the real object store (HTTP behaviour)")
    return store


@pytest.fixture
def browser(store: ObjectStore, http: httpx.Client) -> Browser:
    return Browser(store, http)


@pytest.fixture
def key(store: ObjectStore) -> Iterator[str]:
    """A fresh original key; deleted afterwards."""
    k = original_key(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    yield k
    store.delete_object(k)
