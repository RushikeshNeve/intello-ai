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
# Filled in from the RushDB spike's verified API patterns (docs/spikes/rushdb-validation.md).


def rushdb_seed() -> None:
    raise NotImplementedError("pending RushDB API patterns from M00.03.0x")


def rushdb_digest() -> dict[str, Any]:
    raise NotImplementedError("pending RushDB API patterns from M00.03.0x")


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
    _ = httpx  # used by the RushDB section
    raise SystemExit(main(sys.argv))
