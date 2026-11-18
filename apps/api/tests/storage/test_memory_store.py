"""Unit tests: the memory-only behaviour of InMemoryObjectStore (browser emulation, clock).

The shared put/get/head/delete/multipart semantics are covered by the port-level suite in
``tests/integration/m02_storage`` which runs against this store too.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from app.infrastructure.object_storage.memory import (
    InMemoryObjectStore,
    SimulatedRequestRejected,
)
from app.modules.storage.ports import ChecksumMismatch


def sha(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_fake_urls_are_deterministic_and_on_the_invalid_host() -> None:
    clock = Clock()
    a, b = InMemoryObjectStore(clock=clock), InMemoryObjectStore(clock=clock)
    ra = a.presign_put(
        "org/x/original", content_type="text/plain", sha256_b64=sha(b"x"), expires_seconds=60
    )
    rb = b.presign_put(
        "org/x/original", content_type="text/plain", sha256_b64=sha(b"x"), expires_seconds=60
    )
    assert ra.url == rb.url
    assert ra.url.startswith("http://memory.invalid/org/x/original?")
    assert ra.expires_at == clock.now + timedelta(seconds=60)
    assert ra.headers == {"Content-Type": "text/plain", "x-amz-checksum-sha256": sha(b"x")}


def test_simulate_upload_happy_path_and_failures() -> None:
    store = InMemoryObjectStore()
    data = b"file bytes"
    store.presign_put("k", content_type="text/plain", sha256_b64=sha(data), expires_seconds=60)
    with pytest.raises(ChecksumMismatch):
        store.simulate_upload("k", b"other bytes", "text/plain")
    with pytest.raises(SimulatedRequestRejected) as rejected:
        store.simulate_upload("k", data, "text/html")
    assert rejected.value.status == 403
    assert store.head_object("k") is None
    info = store.simulate_upload("k", data, "text/plain")
    assert (info.size, info.content_type, info.checksum_sha256_b64) == (10, "text/plain", sha(data))
    with pytest.raises(SimulatedRequestRejected):
        store.simulate_upload("never-issued", data, "text/plain")


def test_expired_url_is_denied() -> None:
    clock = Clock()
    store = InMemoryObjectStore(clock=clock)
    req = store.presign_put("k", content_type="a/b", sha256_b64=sha(b"x"), expires_seconds=10)
    clock.now += timedelta(seconds=11)
    assert store.simulate_put(req, b"x").status == 403


def test_tampered_url_is_denied() -> None:
    store = InMemoryObjectStore()
    req = store.presign_put("k", content_type="a/b", sha256_b64=sha(b"x"), expires_seconds=10)
    forged = type(req)(
        req.method, req.url[:-1] + ("0" if req.url[-1] != "0" else "1"), req.headers, req.expires_at
    )
    assert store.simulate_put(forged, b"x").status == 403


def test_simulate_get_disposition_type_and_deletion() -> None:
    store = InMemoryObjectStore()
    store.put_object("k", b"body", content_type="text/html", sha256_b64=sha(b"body"))
    url = store.presign_get(
        "k", expires_seconds=60, filename="a b.html", content_type="text/plain", inline=False
    )
    resp = store.simulate_get(url)
    assert (resp.status, resp.body) == (200, b"body")
    assert resp.headers["content-type"] == "text/plain"
    assert resp.headers["content-disposition"].startswith("attachment;")
    store.delete_object("k")
    assert store.simulate_get(url).status == 404


def test_simulated_multipart_part_upload() -> None:
    store = InMemoryObjectStore()
    up = store.create_multipart("k", content_type="application/octet-stream")
    chunk = b"z" * (5 * 1024 * 1024)
    req = store.presign_upload_part("k", up.upload_id, 1, expires_seconds=60, sha256_b64=sha(chunk))
    assert store.simulate_put(req, b"wrong").status == 400
    ok = store.simulate_put(req, chunk)
    assert ok.status == 200
    assert ok.headers["etag"]
    assert [p.part_number for p in store.list_parts("k", up.upload_id)] == [1]
