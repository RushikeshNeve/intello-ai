"""M05 read-adapter integration: a REAL RushDB 3.0.10 next to the in-memory store.

    UKOS_TEST_COMPOSE_PROJECT=ukos-m05-c1 RUSHDB_HTTP_HOST_PORT=33361 \
        bash scripts/dev/linux-runner/run.sh apps/api \
        "python -m pytest -q -p no:cacheprovider ../../tests/integration/m05_integration"
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("UKOS_TEST_COMPOSE_PROJECT", "ukos-m05-c1")
os.environ.setdefault("RUSHDB_HTTP_HOST_PORT", "33361")

REPO_ROOT = Path(__file__).resolve().parents[3]
for extra in (REPO_ROOT / "tests" / "infrastructure", REPO_ROOT / "apps" / "api"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from compose_support import ComposeProject, compose_file, wait_until
from pydantic import SecretStr

from app.infrastructure.rushdb.settings import RushDbSettings

RUSHDB_PORT = os.environ["RUSHDB_HTTP_HOST_PORT"]
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
