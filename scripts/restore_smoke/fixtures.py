"""Seed and digest fixtures for the backup/restore smoke test (M00.03.05).

Usage (run by scripts/restore_smoke.sh, never against a non-test project):

    python -I scripts/restore_smoke/fixtures.py seed   > seed.json
    python -I scripts/restore_smoke/fixtures.py digest > digest.json

Every store yields a deterministic digest computed from its *contents* (not from
backup archives), so "restored data matches fixture checksums" means the restored
stores contain exactly what was seeded.

Connection settings come from environment variables:
    SMOKE_PG_DSN, SMOKE_RUSHDB_URL, SMOKE_RUSHDB_TOKEN,
    SMOKE_S3_ENDPOINT, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_REGION,
    S3_BUCKET_ORIGINALS
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from typing import Any

import boto3
import httpx
import psycopg
from botocore.config import Config

FIXTURE_TABLE = "restore_smoke_fixture"
OBJECT_PREFIX = "org/restore-smoke/"
N_ROWS = 250
N_OBJECTS = 12


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable {name}")
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object_body(i: int) -> bytes:
    # Deterministic, non-trivial binary content of varying size (1 KiB .. ~300 KiB).
    seed = hashlib.sha256(f"restore-smoke-object-{i}".encode()).digest()
    return (seed * ((i * 800) // len(seed) + 32))[: 1024 + i * 25_000]


# --- PostgreSQL --------------------------------------------------------------


def pg_seed() -> None:
    with psycopg.connect(_env("SMOKE_PG_DSN"), autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {FIXTURE_TABLE}")
        conn.execute(
            f"CREATE TABLE {FIXTURE_TABLE} ("
            " id integer PRIMARY KEY, title text NOT NULL, payload jsonb NOT NULL,"
            " checksum bytea NOT NULL)"
        )
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {FIXTURE_TABLE} (id, title, payload, checksum) VALUES (%s, %s, %s, %s)",
                [
                    (
                        i,
                        f"Fixture document {i} — ünïcødé ✓",
                        json.dumps({"n": i, "tags": [f"t{i % 7}", f"w{i % 3}"]}),
                        hashlib.sha256(str(i).encode()).digest(),
                    )
                    for i in range(N_ROWS)
                ],
            )


def pg_digest() -> dict[str, Any]:
    with psycopg.connect(_env("SMOKE_PG_DSN")) as conn:
        row = conn.execute(
            f"SELECT count(*), md5(string_agg(id::text || '|' || title || '|' || payload::text"
            f" || '|' || encode(checksum, 'hex'), E'\\n' ORDER BY id)) FROM {FIXTURE_TABLE}"
        ).fetchone()
    assert row is not None
    return {"rows": row[0], "md5": row[1]}


# --- Object store ------------------------------------------------------------


def _s3() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=_env("SMOKE_S3_ENDPOINT"),
        aws_access_key_id=_env("S3_ACCESS_KEY_ID"),
        aws_secret_access_key=_env("S3_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("S3_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )


def s3_seed() -> None:
    s3, bucket = _s3(), _env("S3_BUCKET_ORIGINALS")
    for i in range(N_OBJECTS):
        s3.put_object(Bucket=bucket, Key=f"{OBJECT_PREFIX}doc-{i:03d}/v1/original.bin", Body=_object_body(i))


def s3_digest() -> dict[str, Any]:
    s3, bucket = _s3(), _env("S3_BUCKET_ORIGINALS")
    keys = sorted(
        obj["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=OBJECT_PREFIX)
        for obj in page.get("Contents", [])
    )
    per_object = {k: _sha256(s3.get_object(Bucket=bucket, Key=k)["Body"].read()) for k in keys}
    expected = {f"{OBJECT_PREFIX}doc-{i:03d}/v1/original.bin": _sha256(_object_body(i)) for i in range(N_OBJECTS)}
    return {
        "objects": len(keys),
        "sha256": _sha256(json.dumps(per_object, sort_keys=True).encode()),
        "matches_generator": per_object == expected,
    }


# --- RushDB ------------------------------------------------------------------
# Wire calls mirror tests/spikes/rushdb_support.py (Protocol v1, multitenant node):
# every request carries the X-RushDB-Tenant header AND context.scope.

RUSHDB_SCOPE = "ukos-restore-smoke"
VEC_INDEX = "smoke-emb"
TEXT_INDEX = "smoke-text"
N_CHUNKS = 40
VEC_DIMS = 8


def _chunk_vector(i: int) -> list[float]:
    raw = hashlib.sha256(f"restore-smoke-vec-{i}".encode()).digest()[:VEC_DIMS]
    return [round(b / 255.0, 6) for b in raw]


def _rushdb(operation: dict[str, Any]) -> dict[str, Any]:
    resp = httpx.post(
        f"{_env('SMOKE_RUSHDB_URL')}/protocol/v1/execute",
        json={"protocolVersion": 1, "operation": operation, "context": {"scope": RUSHDB_SCOPE}},
        headers={"Authorization": f"Bearer {_env('SMOKE_RUSHDB_TOKEN')}", "X-RushDB-Tenant": RUSHDB_SCOPE},
        timeout=60.0,
    )
    if resp.status_code != 200:
        raise SystemExit(f"RushDB error HTTP {resp.status_code}: {resp.text[:500]}")
    body: dict[str, Any] = resp.json()["response"]
    return body


def _put(label: str, properties: dict[str, Any], vectors: list[dict[str, Any]] | None = None) -> str:
    mutation: dict[str, Any] = {"kind": "putRecord", "mode": "create", "label": label, "properties": properties, "options": {}}
    if vectors:
        mutation["vectors"] = vectors
    record_id: str = _rushdb({"kind": "mutate", "mutation": mutation})["record"]["id"]
    return record_id


def _wait_index_ready(name: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        indexes = _rushdb({"kind": "describe", "describe": {"kind": "indexes"}}).get("indexes", [])
        if any(i.get("name") == name and i.get("state") == "ready" for i in indexes):
            return
        time.sleep(0.2)
    raise SystemExit(f"RushDB index {name} not ready after {timeout}s")


def rushdb_seed() -> None:
    _rushdb({"kind": "index", "index": {"kind": "createVector", "definition": {
        "name": VEC_INDEX, "label": "SmokeChunk", "property": "embedding", "dimensions": VEC_DIMS,
        "similarity": "cosine", "model": "restore-smoke-v1"}}})
    doc_id = _put("SmokeDocument", {"title": "Restore smoke document", "workspace_id": "ws-smoke"})
    chunk_ids = [
        _put(
            "SmokeChunk",
            {"key": f"c{i:03d}", "text": f"restore smoke chunk {i} topic {i % 5}", "workspace_id": "ws-smoke"},
            vectors=[{"propertyName": "embedding", "vector": _chunk_vector(i), "index": VEC_INDEX}],
        )
        for i in range(N_CHUNKS)
    ]
    _rushdb({"kind": "mutate", "mutation": {"kind": "linkRecordsById", "command": {
        "relationType": "HAS_CHUNK", "direction": "out", "sourceIds": [doc_id], "targetIds": chunk_ids}}})
    entity_id = _put("SmokeEntity", {"name": "Topic three"})
    _rushdb({"kind": "mutate", "mutation": {"kind": "linkRecordsById", "command": {
        "relationType": "MENTIONS", "direction": "out", "sourceIds": chunk_ids[3::5], "targetIds": [entity_id]}}})
    _rushdb({"kind": "index", "index": {"kind": "create", "definition": {
        "name": TEXT_INDEX, "label": "SmokeChunk", "property": "text", "kind": "text", "consistency": "immediate"}}})
    _wait_index_ready(TEXT_INDEX)


def rushdb_digest() -> dict[str, Any]:
    chunks = _rushdb({"kind": "query", "query": {"kind": "records", "query": {
        "labels": ["SmokeChunk"], "limit": 1000, "order": {"key": "asc"}}}})["records"]
    mentions = _rushdb({"kind": "query", "query": {"kind": "records", "query": {
        "labels": ["SmokeChunk"], "limit": 1000, "order": {"key": "asc"},
        "predicate": {"SmokeEntity": {"$relation": {"type": "MENTIONS", "direction": "out"}, "name": "Topic three"}}}}})["records"]
    vector = _rushdb({"kind": "query", "query": {"kind": "vector", "query": {
        "labels": ["SmokeChunk"], "property": "embedding", "index": VEC_INDEX,
        "vector": _chunk_vector(7), "limit": 5, "mode": "exact"}}})["records"]
    text = _rushdb({"kind": "query", "query": {"kind": "text", "index": TEXT_INDEX, "query": "topic 3", "limit": 10}})["rows"]
    rels = _rushdb({"kind": "query", "query": {"kind": "relationships", "query": {"relationType": "HAS_CHUNK"}}})["relationships"]
    observed = {
        "chunks": [(r["id"], r["properties"]) for r in chunks],
        "mentions": [r["id"] for r in mentions],
        "vector": [(r["id"], round(r["score"], 6)) for r in vector],
        "text": [row["id"] for row in text],
        "rels": sorted((r["sourceId"], r["targetId"]) for r in rels),
    }
    return {
        "chunks": len(chunks),
        "mentions": len(mentions),
        "has_chunk_rels": len(rels),
        "sha256": _sha256(json.dumps(observed, sort_keys=True, default=str).encode()),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"seed", "digest"}:
        print(__doc__, file=sys.stderr)
        return 2
    if argv[1] == "seed":
        pg_seed()
        s3_seed()
        rushdb_seed()
        print(json.dumps({"seeded": True}))
        return 0
    print(json.dumps({"postgres": pg_digest(), "objects": s3_digest(), "rushdb": rushdb_digest()}, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
