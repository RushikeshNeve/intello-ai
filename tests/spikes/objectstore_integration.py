"""M00.02.04 integration test for infrastructure/compose/objects.yml.

Boots the `ukos-objectstore` compose project, runs the provider-agnostic contract suite against
127.0.0.1:9300 for both buckets, checks that data survives `down` (no -v) + `up`, checks that
re-running `objectstore-init` is harmless, then tears everything down with `down -v`.

    uv run --no-project --with pytest --with boto3 --with httpx \
        python -m pytest -q -rA tests/spikes/objectstore_integration.py

Env overrides: OBJSTORE_IT_PROJECT (default ukos-objectstore), OBJSTORE_IT_ENV_FILE
(default infrastructure/env/.env.example), OBJSTORE_IT_KEEP=1 to skip the final `down -v`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from botocore.client import BaseClient

from objectstore_support import StoreConfig, make_client, sha256_b64

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO / "infrastructure" / "compose" / "objects.yml"
CONTRACT = Path(__file__).resolve().parent / "objectstore_contract.py"
PROJECT = os.environ.get("OBJSTORE_IT_PROJECT", "ukos-objectstore")
ENV_FILE = Path(os.environ.get("OBJSTORE_IT_ENV_FILE", REPO / "infrastructure" / "env" / ".env.example"))


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


ENV = _parse_env(ENV_FILE)
ENDPOINT = f"http://{ENV.get('UKOS_BIND_HOST', '127.0.0.1')}:{ENV.get('OBJECT_STORE_HOST_PORT', '9300')}"
BUCKETS = [ENV["S3_BUCKET_ORIGINALS"], ENV["S3_BUCKET_ARTIFACTS"]]
ORIGINS = [o.strip() for o in ENV.get("API_CORS_ORIGINS", "").split(",") if o.strip()]


def compose(*args: str, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "compose", "-p", PROJECT, "--env-file", str(ENV_FILE), "-f", str(COMPOSE_FILE), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO)
    if check and proc.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)} -> {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc


def _cfg(bucket: str | None = None) -> StoreConfig:
    return StoreConfig(
        provider="objectstore",
        endpoint=ENDPOINT,
        access_key=ENV["S3_ACCESS_KEY_ID"],
        secret_key=ENV["S3_SECRET_ACCESS_KEY"],
        region=ENV.get("S3_REGION", "us-east-1"),
        bucket=bucket,
        cors_origin=ORIGINS[0] if ORIGINS else "http://127.0.0.1:3300",
    )


def _up_and_wait_for_init() -> None:
    """`up --wait` only waits for objectstore health; a one-shot counts as ready once *started*.

    `docker compose wait` blocks until objectstore-init exits and returns its exit code.
    """
    compose("up", "-d", "--wait")
    done = compose("wait", "objectstore-init", check=False)
    assert done.returncode == 0 and done.stdout.strip().endswith("0"), (done.stdout, done.stderr)


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    compose("down", "-v", check=False)
    _up_and_wait_for_init()
    try:
        yield
    finally:
        if os.environ.get("OBJSTORE_IT_KEEP") != "1":
            compose("down", "-v", check=False)


@pytest.fixture(scope="module")
def s3(stack: None) -> BaseClient:
    return make_client(_cfg())


def test_init_created_private_buckets_with_cors(s3: BaseClient) -> None:
    names = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    for bucket in BUCKETS:
        assert bucket in names
        anon = httpx.get(f"{ENDPOINT}/{bucket}", timeout=10)
        assert anon.status_code in (401, 403), anon.text[:200]
        rules = s3.get_bucket_cors(Bucket=bucket)["CORSRules"]
        assert sorted(rules[0]["AllowedOrigins"]) == sorted(ORIGINS)
        assert "*" not in rules[0]["AllowedOrigins"]
    init = compose("ps", "-a", "--format", "{{.Service}} {{.State}} {{.ExitCode}}", "objectstore-init")
    assert "exited 0" in init.stdout, init.stdout


@pytest.mark.parametrize("bucket", BUCKETS)
def test_contract_suite_against_bucket(stack: None, bucket: str) -> None:
    env = {
        **os.environ,
        "OBJSTORE_PROVIDER": "objectstore",
        "OBJSTORE_ENDPOINT": ENDPOINT,
        "OBJSTORE_ACCESS_KEY": ENV["S3_ACCESS_KEY_ID"],
        "OBJSTORE_SECRET_KEY": ENV["S3_SECRET_ACCESS_KEY"],
        "OBJSTORE_REGION": ENV.get("S3_REGION", "us-east-1"),
        "OBJSTORE_BUCKET": bucket,
        "OBJSTORE_CORS_ORIGIN": ORIGINS[0] if ORIGINS else "http://127.0.0.1:3300",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider", str(CONTRACT)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        timeout=900,
    )
    print(proc.stdout[-4000:])
    assert proc.returncode == 0, proc.stdout[-6000:] + proc.stderr[-2000:]
    assert " failed" not in proc.stdout and "SKIPPED" not in proc.stdout


def test_data_survives_down_and_up(s3: BaseClient) -> None:
    key = f"org/{uuid.uuid4().hex}/doc/persist/v1/original.bin"
    data = os.urandom(512 * 1024)
    for bucket in BUCKETS:
        s3.put_object(Bucket=bucket, Key=key, Body=data, ChecksumSHA256=sha256_b64(data), IfNoneMatch="*")

    compose("down")  # no -v: the named volume must keep the bytes
    _up_and_wait_for_init()  # init re-runs on every `up`; it must not touch existing data

    fresh = make_client(_cfg())
    for bucket in BUCKETS:
        got = fresh.get_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        assert got["Body"].read() == data
        assert got.get("ChecksumSHA256") == sha256_b64(data)
        assert sorted(fresh.get_bucket_cors(Bucket=bucket)["CORSRules"][0]["AllowedOrigins"]) == sorted(ORIGINS)
        fresh.delete_object(Bucket=bucket, Key=key)


def test_init_rerun_is_idempotent(s3: BaseClient) -> None:
    key = f"org/{uuid.uuid4().hex}/doc/sentinel/v1/original.bin"
    for bucket in BUCKETS:
        s3.put_object(Bucket=bucket, Key=key, Body=b"sentinel")

    for _ in range(2):
        out = compose("run", "--rm", "objectstore-init")
        log = out.stdout + out.stderr
        for bucket in BUCKETS:
            assert f"bucket {bucket} exists" in log, log
        assert "objectstore-init: done" in log, log

    for bucket in BUCKETS:
        assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == b"sentinel"
        rules = s3.get_bucket_cors(Bucket=bucket)["CORSRules"]
        assert len(rules) == 1 and sorted(rules[0]["AllowedOrigins"]) == sorted(ORIGINS)
        s3.delete_object(Bucket=bucket, Key=key)
