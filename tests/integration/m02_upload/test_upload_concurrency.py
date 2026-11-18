"""Races: finalize, version numbering, the pending cap and the org quota (real PostgreSQL)."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import TypeVar

from upload_support import Scene, UploadApi, body_for

T = TypeVar("T")
Limits = Callable[..., None]
THREADS = 6


def _race[T](jobs: list[Callable[[], T]]) -> list[T]:
    """Run every job at (nearly) the same instant on its own thread."""
    barrier = threading.Barrier(len(jobs))

    def run(job: Callable[[], T]) -> T:
        barrier.wait(timeout=30)
        return job()

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(run, job) for job in jobs]
        return [f.result(timeout=120) for f in futures]


def test_concurrent_finalizes_of_one_session_create_one_version(
    api: UploadApi, scene: Scene
) -> None:
    session = api.session(scene, scene.editor, b"race me")
    results = _race(
        [lambda: api.finalize(scene, scene.editor, session["upload_id"]) for _ in range(THREADS)]
    )
    assert Counter(r.status for r in results) == {201: 1, 200: THREADS - 1}
    assert len({str(r.body) for r in results}) == 1  # everyone got the same document and version
    assert api.count("documents", "id = :d", d=session["document_id"]) == 1
    assert len(api.versions(session["document_id"])) == 1
    audit = api.count(
        "audit_events",
        "target_id = :d AND event_type = 'document.created'",
        d=session["document_id"],
    )
    assert audit == 1


def test_concurrent_new_version_finalizes_get_distinct_consecutive_numbers(
    api: UploadApi, scene: Scene
) -> None:
    _, first = api.uploaded(scene, scene.editor, b"v1")
    doc = first.body["document"]["id"]
    sessions = [
        api.session(scene, scene.editor, f"version {i}".encode(), document_id=doc)
        for i in range(THREADS)
    ]
    results = _race([partial(api.finalize, scene, scene.editor, s["upload_id"]) for s in sessions])
    assert [r.status for r in results] == [201] * THREADS, [r.body for r in results]
    numbers = sorted(r.body["version"]["version_number"] for r in results)
    assert numbers == list(range(2, THREADS + 2))
    assert [v["version_number"] for v in api.versions(doc)] == list(range(1, THREADS + 2))
    latest = api.rows(
        "SELECT v.version_number FROM documents d JOIN document_versions v"
        " ON v.id = d.latest_version_id WHERE d.id = :d",
        d=doc,
    )
    assert latest == [{"version_number": THREADS + 1}]
    assert (
        api.count(
            "audit_events", "target_id = :d AND event_type = 'document.version.created'", d=doc
        )
        == THREADS
    )


def test_concurrent_first_uploads_create_a_single_upload_source(
    api: UploadApi, scene: Scene
) -> None:
    sessions = [api.session(scene, scene.editor, f"doc {i}".encode()) for i in range(THREADS)]
    results = _race([partial(api.finalize, scene, scene.editor, s["upload_id"]) for s in sessions])
    assert [r.status for r in results] == [201] * THREADS, [r.body for r in results]
    assert api.count("sources", "workspace_id = :w AND kind = 'upload'", w=scene.ws) == 1
    assert api.count("documents", "workspace_id = :w", w=scene.ws) == THREADS


def test_concurrent_session_creation_cannot_exceed_the_pending_cap(
    api: UploadApi, scene: Scene, limits: Limits
) -> None:
    limits(UPLOAD_MAX_PENDING_PER_USER=3)
    results = _race(
        [partial(api.create, scene, scene.editor, body_for(f"file {i}".encode())) for i in range(8)]
    )
    assert Counter(r.status for r in results) == {201: 3, 429: 5}
    assert (
        api.count("upload_sessions", "created_by = :u AND state = 'pending'", u=scene.editor.id)
        == 3
    )


def test_concurrent_session_creation_cannot_exceed_the_org_quota(
    api: UploadApi, scene: Scene, limits: Limits
) -> None:
    limits(ORG_STORAGE_QUOTA_BYTES=100)
    users = [scene.editor, scene.ws_admin, scene.owner, scene.org_admin]
    results = _race(
        [
            partial(api.create, scene, users[i % len(users)], body_for(bytes([65 + i]) * 40))
            for i in range(THREADS)
        ]
    )
    statuses: dict[int, int] = dict(Counter(r.status for r in results))
    assert statuses == {201: 2, 409: THREADS - 2}, statuses
    total = api.scalar(
        "SELECT sum(size_bytes) FROM upload_sessions WHERE org_id = :o", o=scene.org_id
    )
    assert total == 80
