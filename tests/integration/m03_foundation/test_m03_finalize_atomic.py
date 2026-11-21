"""M03.01.01 acceptance: "Atomic create job + outbox transaction test passes".

Finalize creates the version AND the ingestion job, its ``queued`` event and the outbox row in ONE
database transaction (contract §2); a rolled-back finalize leaves none of them; an idempotent
repeat does not enqueue again. Deleting a document emits ``document.deleted`` the same way. The
real FastAPI app runs on the app role against a real PostgreSQL.
"""

from __future__ import annotations

import uuid
from typing import Any, NoReturn

import pytest
from upload_support import Scene, UploadApi, sha_hex

from app.core.config import get_settings
from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.modules.documents import catalog_service, finalize
from app.modules.documents.schemas import sha256_hex_to_b64
from app.modules.ingestion.ids import PIPELINE_VERSION, revision_id_for
from app.modules.storage.keys import original_key

DATA = b"the quarterly numbers"


def _ingestion_rows(api: UploadApi, document_id: str) -> dict[str, list[dict[str, Any]]]:
    jobs = api.rows("SELECT * FROM ingestion_jobs WHERE document_id = :d", d=document_id)
    return {
        "jobs": jobs,
        "events": api.rows(
            "SELECT * FROM ingestion_job_events WHERE document_id = :d ORDER BY seq", d=document_id
        ),
        "outbox": api.rows(
            "SELECT * FROM outbox_events WHERE event_type = 'ingestion.job.queued'"
            " AND aggregate_id = ANY(:ids)",
            ids=[j["id"] for j in jobs] or [uuid.uuid4()],
        ),
    }


def _boom(*_: Any, **__: Any) -> NoReturn:
    raise RuntimeError("simulated failure after the job was enqueued")


def test_finalize_creates_version_job_event_and_outbox_row(api: UploadApi, scene: Scene) -> None:
    session = api.session(scene, scene.editor, DATA)
    res = api.finalize(scene, scene.editor, session["upload_id"])
    assert res.status == 201, res.body
    version_id, document_id = session["version_id"], session["document_id"]

    assert len(api.versions(document_id)) == 1
    rows = _ingestion_rows(api, document_id)
    assert len(rows["jobs"]) == len(rows["events"]) == len(rows["outbox"]) == 1
    job, event, outbox = rows["jobs"][0], rows["events"][0], rows["outbox"][0]

    space = get_settings().embedding_space_id
    assert space == "eb3ad37f8d916"
    assert (str(job["org_id"]), str(job["workspace_id"])) == (str(scene.org_id), str(scene.ws))
    assert (str(job["document_id"]), str(job["version_id"])) == (document_id, version_id)
    assert job["revision_id"] == revision_id_for(uuid.UUID(version_id), PIPELINE_VERSION, space)
    assert (job["state"], job["attempts"], job["lease_owner"]) == ("queued", 0, None)
    assert (job["pipeline_version"], job["embedding_space_id"]) == (PIPELINE_VERSION, space)

    assert (event["job_id"], event["event_type"], event["attempt"]) == (job["id"], "queued", 0)
    assert str(event["version_id"]) == version_id

    assert outbox["payload"]["job_id"] == str(job["id"])  # M08 tracing may add a "trace" key
    assert (outbox["aggregate_type"], str(outbox["org_id"])) == ("ingestion_job", str(scene.org_id))
    assert outbox["dispatched_at"] is None


def test_finalize_that_rolls_back_leaves_no_version_job_event_or_outbox(
    api: UploadApi, scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = api.session(scene, scene.editor, DATA)
    document_id = session["document_id"]
    # The audit event is written AFTER the job was enqueued, inside the same transaction.
    monkeypatch.setattr(finalize, "record_event", _boom)
    try:
        res = api.finalize(scene, scene.editor, session["upload_id"])
    except RuntimeError:
        pass  # the simulated fault propagated through the test client
    else:
        assert res.status == 500, res.body

    assert api.versions(document_id) == []
    assert api.rows("SELECT id FROM documents WHERE id = :d", d=document_id) == []
    rows = _ingestion_rows(api, document_id)
    assert rows == {"jobs": [], "events": [], "outbox": []}
    assert api.session_row(session["upload_id"])["state"] == "pending"

    # The client retries after the fault is gone: everything appears, exactly once.
    monkeypatch.undo()
    retry = api.finalize(scene, scene.editor, session["upload_id"])
    assert retry.status == 201, retry.body
    rows = _ingestion_rows(api, document_id)
    assert len(rows["jobs"]) == len(rows["events"]) == len(rows["outbox"]) == 1


def test_repeated_finalize_does_not_enqueue_again(api: UploadApi, scene: Scene) -> None:
    session = api.session(scene, scene.editor, DATA)
    first = api.finalize(scene, scene.editor, session["upload_id"])
    again = api.finalize(scene, scene.editor, session["upload_id"])
    assert (first.status, again.status) == (201, 200)
    rows = _ingestion_rows(api, session["document_id"])
    assert len(rows["jobs"]) == len(rows["events"]) == len(rows["outbox"]) == 1


def test_a_new_version_gets_its_own_job(api: UploadApi, scene: Scene) -> None:
    first = api.finalize(scene, scene.editor, api.session(scene, scene.editor, DATA)["upload_id"])
    document_id = first.body["document"]["id"]
    second_session = api.session(scene, scene.editor, b"version two", document_id=document_id)
    second = api.finalize(scene, scene.editor, second_session["upload_id"])
    assert second.status == 201, second.body
    rows = _ingestion_rows(api, document_id)
    assert len(rows["jobs"]) == len(rows["events"]) == len(rows["outbox"]) == 2
    assert len({j["revision_id"] for j in rows["jobs"]}) == 2
    assert {str(j["version_id"]) for j in rows["jobs"]} == {
        first.body["version"]["id"],
        second.body["version"]["id"],
    }


def test_failed_verification_enqueues_nothing(
    api: UploadApi, scene: Scene, store: InMemoryObjectStore
) -> None:
    """A rejected upload (size mismatch) creates no version, so no job and no outbox row."""
    session = api.session(scene, scene.editor, DATA, put=False)
    other = DATA + b" and more"
    key = original_key(scene.org_id, session["document_id"], session["version_id"])
    store.put_object(
        key, other, content_type="text/plain", sha256_b64=sha256_hex_to_b64(sha_hex(other))
    )
    res = api.finalize(scene, scene.editor, session["upload_id"])
    assert (res.status, res.body) == (409, {"detail": "size_mismatch"})
    assert _ingestion_rows(api, session["document_id"]) == {"jobs": [], "events": [], "outbox": []}


def test_deleting_a_document_emits_document_deleted_atomically(
    api: UploadApi, scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    finalized = api.finalize(
        scene, scene.editor, api.session(scene, scene.editor, DATA)["upload_id"]
    )
    document_id = finalized.body["document"]["id"]
    path = f"{scene.base}/documents/{document_id}"

    def deleted_events() -> list[dict[str, Any]]:
        return api.rows(
            "SELECT * FROM outbox_events WHERE event_type = 'document.deleted'"
            " AND aggregate_id = :d",
            d=document_id,
        )

    # A failure after the event was enqueued rolls the whole deletion back.
    monkeypatch.setattr(catalog_service, "audit", _boom)
    try:
        res = api.delete(path, scene.owner)
    except RuntimeError:
        pass  # the simulated fault propagated through the test client
    else:
        assert res.status == 500, res.body
    assert deleted_events() == []
    assert (
        api.rows("SELECT deleted_at FROM documents WHERE id = :d", d=document_id)[0]["deleted_at"]
        is None
    )
    monkeypatch.undo()

    # The real deletion commits the logical deletion and the event together.
    res = api.delete(path, scene.owner)
    assert res.status == 202, res.body
    events = deleted_events()
    assert len(events) == 1
    event = events[0]
    assert event["payload"] == {"document_id": document_id, "workspace_id": str(scene.ws)}
    assert (str(event["org_id"]), event["aggregate_type"]) == (str(scene.org_id), "document")
    assert event["dispatched_at"] is None

    # A second DELETE is a 404 and emits nothing.
    assert api.delete(path, scene.owner).status == 404
    assert len(deleted_events()) == 1
