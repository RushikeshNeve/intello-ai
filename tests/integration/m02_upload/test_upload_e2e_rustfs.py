"""End to end against the REAL object store (RustFS): API in-process + the real ``S3ObjectStore``.

create session -> PUT the bytes with httpx using exactly ``upload.headers`` -> finalize -> the
version exists and HEAD matches; a tampered upload (other bytes, same signed headers) is rejected
by the store itself and can never be finalized.

The object store runs from ``infrastructure/compose/objects.yml`` in the same Compose project as
PostgreSQL (``ukos-m02-upload``), host port ``OBJECT_STORE_HOST_PORT`` (39311).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import httpx
import pytest
from compose_support import ComposeProject, compose_file, wait_until
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from tenancy_support import FakeVerifier
from upload_support import Scene, StoreHolder, UploadApi, body_for, sha_hex

from app.infrastructure.object_storage.s3 import S3ObjectStore
from app.infrastructure.object_storage.settings import ObjectStoreSettings
from app.modules.documents.schemas import sha256_hex_to_b64
from app.modules.storage.keys import original_key

PROJECT = os.environ["UKOS_TEST_COMPOSE_PROJECT"] + "-objs"  # own project: down -v spares Postgres
PORT = os.environ["OBJECT_STORE_HOST_PORT"]
PUBLIC_URL = f"http://127.0.0.1:{PORT}"
DATA = b"real bytes for the real store\n" * 50


@pytest.fixture(scope="module")
def s3(stack: ComposeProject) -> Iterator[S3ObjectStore]:
    """RustFS with both buckets created (``objectstore-init``), torn down with ``down -v``."""
    project = ComposeProject(
        [compose_file("objects.yml")],
        project=PROJECT,
        extra_env={"OBJECT_STORE_HOST_PORT": PORT, "S3_PUBLIC_ENDPOINT_URL": PUBLIC_URL},
    )
    try:
        project.up("objectstore", wait=True)
        project.run("run", "--rm", "objectstore-init")
        env = project.env
        settings = ObjectStoreSettings(
            S3_ENDPOINT_URL=PUBLIC_URL,  # the tests run on the host network, not in compose's
            S3_PUBLIC_ENDPOINT_URL=PUBLIC_URL,
            S3_REGION=env["S3_REGION"],
            S3_ACCESS_KEY_ID=SecretStr(env["S3_ACCESS_KEY_ID"]),
            S3_SECRET_ACCESS_KEY=SecretStr(env["S3_SECRET_ACCESS_KEY"]),
            S3_BUCKET_ORIGINALS=env["S3_BUCKET_ORIGINALS"],
            S3_BUCKET_ARTIFACTS=env["S3_BUCKET_ARTIFACTS"],
        )
        store = S3ObjectStore(settings)
        wait_until(
            lambda: store.head_object("probe") is None, timeout=30, description="S3 endpoint"
        )
        yield store
    finally:
        if os.environ.get("UKOS_TEST_KEEP_STACK") != "1":
            project.down(volumes=True)


@pytest.fixture
def real(
    s3: S3ObjectStore,
    holder: StoreHolder,
    client: TestClient,
    verifier: FakeVerifier,
    admin_engine: Engine,
) -> UploadApi:
    holder.store = s3
    return UploadApi(client, verifier, admin_engine, s3)


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        yield client


def test_upload_with_exact_headers_then_finalize(
    real: UploadApi, s3: S3ObjectStore, http: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("S3_PRESIGN_TTL_SECONDS", "300")
    scene: Scene = real.scene()
    created = real.create(scene, scene.editor, body_for(DATA, filename="real.txt"))
    assert created.status == 201, created.body
    upload = created.body["upload"]
    assert upload["url"].startswith(f"{PUBLIC_URL}/")
    assert upload["headers"]["x-amz-checksum-sha256"] == sha256_hex_to_b64(sha_hex(DATA))
    assert "real.txt" not in upload["url"]
    key = original_key(scene.org_id, created.body["document_id"], created.body["version_id"])

    # Nothing there yet: finalize says so.
    assert real.finalize(scene, scene.editor, created.body["upload_id"]).body == {
        "detail": "object_missing"
    }

    put = http.put(upload["url"], content=DATA, headers=upload["headers"])
    assert put.status_code == 200, put.text

    done = real.finalize(scene, scene.editor, created.body["upload_id"])
    assert done.status == 201, done.body
    version = done.body["version"]
    assert (version["version_number"], version["size_bytes"], version["sha256"]) == (
        1,
        len(DATA),
        sha_hex(DATA),
    )
    assert done.body["document"]["title"] == "real"

    info = s3.head_object(key)
    assert info is not None
    assert info.size == len(DATA)
    assert info.checksum_sha256_b64 == sha256_hex_to_b64(sha_hex(DATA))
    assert real.versions(created.body["document_id"])[0]["object_key"] == key
    assert real.finalize(scene, scene.editor, created.body["upload_id"]).status == 200

    # A second version of the same document through the same path.
    more = DATA + b"more"
    second = real.create(
        scene, scene.editor, body_for(more, document_id=created.body["document_id"])
    )
    assert (
        http.put(
            second.body["upload"]["url"], content=more, headers=second.body["upload"]["headers"]
        ).status_code
        == 200
    )
    again = real.finalize(scene, scene.editor, second.body["upload_id"])
    assert again.status == 201
    assert again.body["version"]["version_number"] == 2
    s3.delete_object(key)
    s3.delete_object(
        original_key(scene.org_id, created.body["document_id"], second.body["version_id"])
    )


def test_tampered_upload_is_rejected_by_the_store_and_never_finalizes(
    real: UploadApi, s3: S3ObjectStore, http: httpx.Client
) -> None:
    scene = real.scene()
    created = real.create(scene, scene.editor, body_for(DATA))
    assert created.status == 201, created.body
    upload = created.body["upload"]
    key = original_key(scene.org_id, created.body["document_id"], created.body["version_id"])

    # Other bytes under the signed checksum header: the store refuses the body.
    tampered = http.put(upload["url"], content=b"T" * len(DATA), headers=upload["headers"])
    assert tampered.status_code == 400, tampered.text
    # A different Content-Type than the signed one: the signature no longer matches.
    wrong_type = dict(upload["headers"], **{"Content-Type": "text/html"})
    assert http.put(upload["url"], content=DATA, headers=wrong_type).status_code == 403
    # A different checksum header than the signed one.
    wrong_sum = dict(
        upload["headers"], **{"x-amz-checksum-sha256": sha256_hex_to_b64(sha_hex(b"o"))}
    )
    assert http.put(upload["url"], content=DATA, headers=wrong_sum).status_code == 403

    assert s3.head_object(key) is None
    res = real.finalize(scene, scene.editor, created.body["upload_id"])
    assert (res.status, res.body) == (409, {"detail": "object_missing"})
    assert real.versions(created.body["document_id"]) == []


def test_bytes_written_behind_the_apis_back_are_caught_at_finalize(
    real: UploadApi, s3: S3ObjectStore
) -> None:
    """Someone with store credentials replaces the object: size/checksum verification catches it."""
    scene = real.scene()
    created = real.create(scene, scene.editor, body_for(DATA))
    key = original_key(scene.org_id, created.body["document_id"], created.body["version_id"])
    forged = b"F" * len(DATA)
    s3.put_object(
        key, forged, content_type="text/plain", sha256_b64=sha256_hex_to_b64(sha_hex(forged))
    )
    res = real.finalize(scene, scene.editor, created.body["upload_id"])
    assert (res.status, res.body) == (409, {"detail": "checksum_mismatch"})
    assert s3.head_object(key) is None  # deleted
    assert real.session_row(created.body["upload_id"])["state"] == "failed"
