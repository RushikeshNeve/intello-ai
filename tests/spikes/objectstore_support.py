"""Shared fixtures and helpers for the provider-agnostic object-store contract suite.

Kept out of ``conftest.py`` on purpose (``tests/conftest.py`` is shared with other agents).
The contract module star-imports this file so pytest discovers the fixtures.

Configuration (all optional; defaults target the M00.02.04 compose stack on 127.0.0.1:9300):

    OBJSTORE_PROVIDER      label printed in reports (e.g. seaweedfs, rustfs, garage)
    OBJSTORE_ENDPOINT      S3 endpoint reachable from the test host (default http://127.0.0.1:9300)
    OBJSTORE_ACCESS_KEY    falls back to S3_ACCESS_KEY_ID, then the .env.example placeholder
    OBJSTORE_SECRET_KEY    falls back to S3_SECRET_ACCESS_KEY, then the .env.example placeholder
    OBJSTORE_REGION        falls back to S3_REGION, then us-east-1
    OBJSTORE_BUCKET        existing bucket to test against; when unset a temporary bucket is created
                           and removed at the end of the session
    OBJSTORE_CORS_ORIGIN   browser origin used for CORS checks (default http://127.0.0.1:3300)

Only the plain S3 API is used (path-style, SigV4) so the same suite applies to Cloudflare R2.
"""

from __future__ import annotations

import base64
import hashlib
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import boto3
import httpx
import pytest
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

MIB = 1024 * 1024

# Placeholders from infrastructure/env/.env.example (not secrets).
_DEFAULT_ACCESS_KEY = "change-me-s3-access-key"
_DEFAULT_SECRET_KEY = "change-me-s3-secret-key"


@dataclass(frozen=True)
class StoreConfig:
    provider: str
    endpoint: str
    access_key: str
    secret_key: str
    region: str
    bucket: str | None
    cors_origin: str


def load_config() -> StoreConfig:
    env = os.environ.get
    return StoreConfig(
        provider=env("OBJSTORE_PROVIDER", "objectstore"),
        endpoint=env("OBJSTORE_ENDPOINT", "http://127.0.0.1:9300").rstrip("/"),
        access_key=env("OBJSTORE_ACCESS_KEY") or env("S3_ACCESS_KEY_ID") or _DEFAULT_ACCESS_KEY,
        secret_key=env("OBJSTORE_SECRET_KEY") or env("S3_SECRET_ACCESS_KEY") or _DEFAULT_SECRET_KEY,
        region=env("OBJSTORE_REGION") or env("S3_REGION") or "us-east-1",
        bucket=env("OBJSTORE_BUCKET") or None,
        cors_origin=env("OBJSTORE_CORS_ORIGIN", "http://127.0.0.1:3300"),
    )


def make_client(cfg: StoreConfig) -> BaseClient:
    """Plain SigV4, path-style client.

    ``request_checksum_calculation="when_required"`` stops botocore >= 1.36 from adding
    default CRC32 trailers (aws-chunked) to every upload; the suite sends explicit
    SHA-256 checksums where it tests them (ADR-010).
    """
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint,
        aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret_key,
        region_name=cfg.region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 2, "mode": "standard"},
            connect_timeout=5,
            read_timeout=60,
        ),
    )


def sha256_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def http_status(exc: ClientError) -> int:
    return int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))


def unsupported(feature: str, detail: str) -> None:
    """Mark an optional feature as unsupported by the provider (reported as a skip)."""
    pytest.skip(f"UNSUPPORTED [{feature}]: {detail}")


def empty_bucket(client: BaseClient, bucket: str, prefix: str = "") -> None:
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            client.delete_object(Bucket=bucket, Key=obj["Key"])
    try:
        uploads = client.list_multipart_uploads(Bucket=bucket, Prefix=prefix).get("Uploads", [])
    except ClientError:
        uploads = []
    for up in uploads:
        try:
            client.abort_multipart_upload(Bucket=bucket, Key=up["Key"], UploadId=up["UploadId"])
        except ClientError:
            pass


def ensure_bucket(client: BaseClient, bucket: str) -> bool:
    """Create ``bucket`` if missing. Returns True when it was created by this call."""
    try:
        client.head_bucket(Bucket=bucket)
        return False
    except ClientError as exc:
        if http_status(exc) not in (404, 403) and error_code(exc) not in ("404", "NoSuchBucket"):
            raise
    try:
        client.create_bucket(Bucket=bucket)
    except ClientError as exc:
        if error_code(exc) in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            return False
        raise
    return True


def cors_rules(origins: list[str]) -> dict:
    return {
        "CORSRules": [
            {
                "AllowedOrigins": origins,
                "AllowedMethods": ["GET", "PUT", "HEAD"],
                "AllowedHeaders": ["*"],
                "ExposeHeaders": ["ETag", "x-amz-checksum-sha256", "x-amz-version-id"],
                "MaxAgeSeconds": 3000,
            }
        ]
    }


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def store_cfg() -> StoreConfig:
    return load_config()


@pytest.fixture(scope="session")
def s3(store_cfg: StoreConfig) -> BaseClient:
    client = make_client(store_cfg)
    try:
        client.list_buckets()
    except Exception as exc:  # noqa: BLE001 - surface a readable reason
        pytest.fail(f"object store {store_cfg.provider} at {store_cfg.endpoint} unreachable: {exc}")
    return client


@pytest.fixture(scope="session")
def http() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        yield client


@pytest.fixture(scope="session")
def bucket(s3: BaseClient, store_cfg: StoreConfig) -> Iterator[str]:
    """Bucket under test. Uses OBJSTORE_BUCKET when set, else a temporary bucket."""
    if store_cfg.bucket:
        ensure_bucket(s3, store_cfg.bucket)
        yield store_cfg.bucket
        return
    name = f"ukos-contract-{uuid.uuid4().hex[:10]}"
    s3.create_bucket(Bucket=name)
    yield name
    empty_bucket(s3, name)
    s3.delete_bucket(Bucket=name)


@pytest.fixture()
def org_prefix(s3: BaseClient, bucket: str) -> Iterator[str]:
    """Unique ``org/<id>/`` key namespace per test; cleaned up afterwards."""
    prefix = f"org/{uuid.uuid4().hex}/"
    yield prefix
    empty_bucket(s3, bucket, prefix)
