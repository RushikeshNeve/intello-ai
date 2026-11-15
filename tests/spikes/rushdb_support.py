"""Shared RushDB 3 spike helpers (M00.02.02 / M00.03.01-03).

Thin raw-HTTP client over the Protocol v1 envelope (`POST /protocol/v1/execute`).
The PyPI `rushdb` SDK is not used: the spikes need exact control over the tenant
header, the envelope scope and error envelopes, and must record the exact wire calls
the M03.03.01 adapter will make.

Environment (defaults match infrastructure/env/.env.example):
  RUSHDB_HTTP_URL  base URL seen from the host   (default http://127.0.0.1:3310)
  RUSHDB_TOKEN     operator/bootstrap token      (default change-me-rushdb-token)

This module is imported by the spike files; it is not a pytest conftest on purpose
(tests/conftest.py is shared with other agents).
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

BASE_URL = os.environ.get(
    "RUSHDB_HTTP_URL",
    f"http://127.0.0.1:{os.environ.get('RUSHDB_HTTP_HOST_PORT', '3310')}",
)
TOKEN = os.environ.get("RUSHDB_TOKEN", "change-me-rushdb-token")

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "infrastructure" / "compose" / "rushdb.yml"
ENV_FILE = REPO_ROOT / "infrastructure" / "env" / ".env.example"
COMPOSE_PROJECT = os.environ.get("RUSHDB_COMPOSE_PROJECT", "ukos-rushdb")


class RushDBError(Exception):
    """A non-200 Protocol v1 response."""

    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        err = body.get("error") if isinstance(body, dict) else body
        self.code = err.get("code") if isinstance(err, dict) else err
        super().__init__(f"HTTP {status}: {body}")


@dataclass
class Tenant:
    """A client bound to ONE tenant scope (an organization in UKOS).

    `scope=None` sends no tenant header and no envelope scope (used only to prove
    fail-closed behaviour; a multitenant node refuses it).
    """

    scope: str | None
    token: str = TOKEN
    base_url: str = BASE_URL
    header_scope: str | None = field(default=None)  # override to test header/envelope mismatch
    _http: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._http = httpx.Client(base_url=self.base_url, timeout=30.0)

    # -- transport ---------------------------------------------------------
    def raw(self, operation: dict, *, scope: Any = ..., header: Any = ..., token: Any = ...) -> httpx.Response:
        scope = self.scope if scope is ... else scope
        header = (self.header_scope or self.scope) if header is ... else header
        token = self.token if token is ... else token
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if header is not None:
            headers["X-RushDB-Tenant"] = header
        env: dict[str, Any] = {"protocolVersion": 1, "operation": operation}
        if scope is not None:
            env["context"] = {"scope": scope}
        return self._http.post("/protocol/v1/execute", json=env, headers=headers)

    def execute(self, operation: dict, **kw: Any) -> dict:
        resp = self.raw(operation, **kw)
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        if resp.status_code != 200:
            raise RushDBError(resp.status_code, body)
        return body["response"]

    # -- writes ------------------------------------------------------------
    def put(self, label: str, properties: dict, *, id: str | None = None, vectors: list | None = None) -> dict:
        m: dict[str, Any] = {"kind": "putRecord", "mode": "create", "label": label,
                             "properties": properties, "options": {"suggestTypes": False}}
        if id:
            m["id"] = id
        if vectors:
            m["vectors"] = vectors
        return self.execute({"kind": "mutate", "mutation": m})["record"]

    def link(self, source_ids: list[str], target_ids: list[str], rel_type: str, properties: dict | None = None) -> dict:
        cmd = {"relationType": rel_type, "direction": "out", "sourceIds": source_ids, "targetIds": target_ids}
        if properties:
            cmd["properties"] = properties
        return self.execute({"kind": "mutate", "mutation": {"kind": "linkRecordsById", "command": cmd}})

    def delete_records(self, labels: list[str], predicate: dict) -> int:
        r = self.execute({"kind": "mutate", "mutation": {"kind": "deleteRecords",
                                                         "query": {"labels": labels, "predicate": predicate}}})
        return r["affected"]

    def create_vector_index(self, name: str, label: str, prop: str, dims: int, similarity: str = "cosine") -> dict:
        return self.execute({"kind": "index", "index": {"kind": "createVector", "definition": {
            "name": name, "label": label, "property": prop, "dimensions": dims,
            "similarity": similarity, "model": "ukos-spike-handmade-v1"}}})

    def upsert_vectors(self, name: str, items: list[tuple[str, list[float]]]) -> dict:
        return self.execute({"kind": "index", "index": {"kind": "upsertVectors", "name": name,
                                                        "items": [{"recordId": i, "vector": v} for i, v in items]}})

    def create_text_index(self, name: str, label: str, prop: str, consistency: str = "immediate") -> dict:
        r = self.execute({"kind": "index", "index": {"kind": "create", "definition": {
            "name": name, "label": label, "property": prop, "kind": "text", "consistency": consistency}}})
        self.wait_index_ready(name)
        return r

    def create_ordered_index(self, name: str, label: str, prop: str) -> dict:
        return self.execute({"kind": "index", "index": {"kind": "create", "definition": {
            "name": name, "label": label, "property": prop}}})

    def wait_index_ready(self, name: str, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            idx = self.execute({"kind": "describe", "describe": {"kind": "indexes"}})
            for i in idx.get("indexes", []):
                if i.get("name") == name and i.get("state") == "ready":
                    return
            if time.monotonic() > deadline:
                raise TimeoutError(f"index {name} not ready: {idx}")
            time.sleep(0.2)

    def purge(self) -> None:
        self.execute({"kind": "purgeTenant"})

    # -- reads -------------------------------------------------------------
    def records(self, labels: list[str], predicate: dict | None = None, limit: int = 1000, **extra: Any) -> dict:
        q: dict[str, Any] = {"labels": labels, "limit": limit}
        if predicate:
            q["predicate"] = predicate
        q.update(extra)
        return self.execute({"kind": "query", "query": {"kind": "records", "query": q}})

    def get(self, record_id: str) -> dict:
        return self.execute({"kind": "query", "query": {"kind": "record", "id": record_id}})["record"]

    def vector(self, index: str, label: str, prop: str, vec: list[float], limit: int,
               predicate: dict | None = None, mode: str = "exact") -> dict:
        q: dict[str, Any] = {"labels": [label], "property": prop, "index": index, "vector": vec,
                             "limit": limit, "mode": mode}
        if predicate:
            q["predicate"] = predicate
        return self.execute({"kind": "query", "query": {"kind": "vector", "query": q}})

    def text(self, index: str, query: str, limit: int = 10) -> dict:
        return self.execute({"kind": "query", "query": {"kind": "text", "index": index, "query": query, "limit": limit}})

    def hybrid(self, label: str, limit: int, *, predicate: dict | None = None, vector: dict | None = None,
               text: dict | None = None, **extra: Any) -> dict:
        q: dict[str, Any] = {"labels": [label], "limit": limit}
        if predicate:
            q["predicate"] = predicate
        if vector:
            q["vector"] = vector
        if text:
            q["text"] = text
        q.update(extra)
        return self.execute({"kind": "query", "query": {"kind": "hybrid", "query": q}})

    def relationships(self, **q: Any) -> dict:
        return self.execute({"kind": "query", "query": {"kind": "relationships", "query": q}})

    def statement(self, language: str, statement: str, parameters: dict | None = None) -> dict:
        q: dict[str, Any] = {"kind": "statement", "language": language, "statement": statement}
        if parameters:
            q["parameters"] = parameters
        return self.execute({"kind": "query", "query": q})

    def describe(self, kind: str, **extra: Any) -> dict:
        return self.execute({"kind": "describe", "describe": {"kind": kind, **extra}})

    def close(self) -> None:
        self._http.close()


def ids(resp: dict) -> list[str]:
    """Record ids of a `records` response, in order."""
    return [r["id"] for r in resp["records"]]


def prop(resp: dict, name: str) -> list[Any]:
    """One property of every record of a `records` response, in order."""
    return [r["properties"].get(name) for r in resp["records"]]


def unique_scope(prefix: str) -> str:
    """Per-run tenant scope so runs never collide (and leftovers are obvious)."""
    return f"ukos-spike-{prefix}-{uuid.uuid4().hex[:8]}"


def reachable() -> bool:
    try:
        return httpx.get(f"{BASE_URL}/ready", timeout=3.0).status_code == 200
    except httpx.HTTPError:
        return False


requires_rushdb = pytest.mark.skipif(
    not reachable(), reason=f"RushDB is not reachable at {BASE_URL} (start infrastructure/compose/rushdb.yml)"
)


def wait_ready(timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if reachable():
            return
        time.sleep(0.5)
    raise TimeoutError(f"RushDB not ready at {BASE_URL} after {timeout}s")


def compose(*args: str, timeout: float = 300.0) -> subprocess.CompletedProcess:
    """Run `docker compose` against THIS project only (never another project's resources)."""
    cmd = ["docker", "compose", "-p", COMPOSE_PROJECT, "--env-file", str(ENV_FILE), "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)


def timed(fn, *a: Any, repeat: int = 20, **kw: Any) -> tuple[Any, float, float]:
    """Run fn repeatedly; return (last result, median ms, p95 ms) of client-observed latency."""
    samples = []
    out = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn(*a, **kw)
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    return out, samples[len(samples) // 2], samples[min(len(samples) - 1, int(len(samples) * 0.95))]


@pytest.fixture
def tenant_factory():
    """Yield a factory of fresh, uniquely scoped tenants; purge them all afterwards."""
    made: list[Tenant] = []

    def make(prefix: str) -> Tenant:
        t = Tenant(unique_scope(prefix))
        made.append(t)
        return t

    yield make
    for t in made:
        try:
            t.purge()
        finally:
            t.close()
