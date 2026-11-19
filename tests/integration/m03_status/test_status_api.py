"""M03.03.04: ``GET/POST {ws}/documents/{id}/ingestion[/retry]`` and ``Document.ingestion``."""

from __future__ import annotations

import json
import threading
import uuid
from typing import Any

import pytest
from catalog_support import World, ensure_source, seed_document
from sqlalchemy import Engine, event, text
from sqlalchemy.engine import Engine as EngineClass
from status_support import (
    FORBIDDEN_FRAGMENTS,
    add_event,
    job_row,
    outbox_count,
    seed_job,
)
from tenancy_support import assert_denied

STATUS_KEYS = {
    "state",
    "stage",
    "attempts_label",
    "error_code",
    "message",
    "updated_at",
    "can_retry",
    "document_id",
    "version_id",
    "job_id",
    "revision_id",
    "attempts",
    "max_attempts",
    "published_at",
    "events",
    "next_after",
}
EVENT_KEYS = {"id", "type", "label", "stage", "attempt", "at"}


def ingestion_path(world: World, doc_id: uuid.UUID | str, sub: str = "") -> str:
    return world.doc_path(doc_id, f"/ingestion{sub}")


def assert_no_internals(payload: Any) -> None:
    text_ = json.dumps(payload)
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment not in text_, fragment


# --- shape and safety --------------------------------------------------------------------------


def test_status_shape_and_no_internals(world: World) -> None:
    doc = world.seed()
    job = seed_job(
        world,
        doc,
        state="running",
        attempts=2,
        stage="embed",
        error_code="storage_unavailable",
        revision_state="building",
        events=(("queued", None, 0), ("started", None, 1), ("stage_started", "extract", 1)),
    )
    res = world.api.get(ingestion_path(world, doc.id), world.viewer)
    assert res.status == 200, res.body
    body = res.body
    assert set(body) == STATUS_KEYS
    assert body["state"] == "processing"
    assert body["stage"] == "embed"
    assert body["attempts_label"] == "Attempt 2 of 5"
    assert body["error_code"] is None  # a running job shows no error
    assert body["attempts"] == 2
    assert body["max_attempts"] == 5
    assert body["document_id"] == str(doc.id)
    assert body["version_id"] == str(doc.versions[-1])
    assert body["job_id"] == str(job.job_id)
    assert body["published_at"] is None
    assert [e["type"] for e in body["events"]] == ["queued", "started", "stage_started"]
    assert all(set(e) == EVENT_KEYS for e in body["events"])
    assert body["events"][2]["stage"] == "extract"
    assert body["events"][0]["label"] == "Queued"
    assert_no_internals(body)


def test_failure_shows_safe_code_and_message_only(world: World) -> None:
    doc = world.seed()
    seed_job(
        world, doc, state="dead", attempts=5, error_code="pdf_unreadable", revision_state="failed"
    )
    body = world.api.get(ingestion_path(world, doc.id), world.viewer).body
    assert body["state"] == "failed"
    assert body["error_code"] == "pdf_unreadable"
    assert "PDF" in body["message"]
    assert body["can_retry"] is False  # a viewer cannot retry
    assert_no_internals(body)


def test_unknown_error_code_is_not_echoed(world: World) -> None:
    doc = world.seed()
    seed_job(world, doc, state="dead", error_code="ValueError: s3://x/y secret")
    body = world.api.get(ingestion_path(world, doc.id), world.editor).body
    assert body["error_code"] == "processing_failed"
    assert body["message"] == "Processing failed."
    assert body["can_retry"] is True
    assert_no_internals(body)


@pytest.mark.parametrize(
    ("job_state", "code", "revision", "state", "retryable"),
    [
        ("queued", None, None, "queued", False),
        ("running", None, "building", "processing", False),
        ("succeeded", None, "published", "ready", False),
        ("succeeded", None, "building", "processing", False),
        ("dead", "index_unavailable", "failed", "failed", True),
        ("unsupported", "unsupported_type", None, "unsupported", True),
        ("unsupported", "no_text_extracted", "failed", "no_text", False),
        ("cancelled", None, None, "cancelled", False),
    ],
)
def test_state_mapping(
    world: World,
    job_state: str,
    code: str | None,
    revision: str | None,
    state: str,
    retryable: bool,
) -> None:
    doc = world.seed()
    seed_job(world, doc, state=job_state, error_code=code, revision_state=revision)
    body = world.api.get(ingestion_path(world, doc.id), world.editor).body
    assert body["state"] == state
    assert body["can_retry"] is retryable
    summary = world.api.get(world.doc_path(doc.id), world.editor).body["ingestion"]
    assert summary["state"] == state
    assert summary["can_retry"] is retryable
    if state in ("failed", "unsupported", "no_text"):
        assert summary["error_code"] == code
        assert summary["message"]
    else:
        assert summary["error_code"] is None


def test_no_job_is_none(world: World) -> None:
    doc = world.seed()
    body = world.api.get(ingestion_path(world, doc.id), world.viewer).body
    assert body["state"] == "none"
    assert body["events"] == []
    assert body["job_id"] is None


def test_queued_after_failed_attempt_is_labelled_as_retry(world: World) -> None:
    doc = world.seed()
    seed_job(world, doc, state="queued", attempts=1, stage="embed", error_code="timeout")
    body = world.api.get(ingestion_path(world, doc.id), world.viewer).body
    assert body["state"] == "queued"
    assert body["attempts_label"] == "Retrying: attempt 2 of 5"
    assert body["error_code"] is None


def test_published_at_only_when_ready(world: World) -> None:
    doc = world.seed()
    seed_job(world, doc, state="succeeded", revision_state="published")
    body = world.api.get(ingestion_path(world, doc.id), world.viewer).body
    assert body["state"] == "ready"
    assert body["published_at"] is not None


def test_status_describes_the_latest_version_only(world: World) -> None:
    doc = world.seed(versions=2)
    seed_job(world, doc, state="dead", error_code="pdf_unreadable", version_id=doc.versions[0])
    assert world.api.get(ingestion_path(world, doc.id), world.viewer).body["state"] == "none"
    seed_job(world, doc, state="succeeded", revision_state="published")
    body = world.api.get(ingestion_path(world, doc.id), world.viewer).body
    assert body["state"] == "ready"
    assert body["version_id"] == str(doc.versions[-1])


# --- authorization --------------------------------------------------------------------------------


def test_read_role_matrix_and_concealment(world: World) -> None:
    doc = world.seed()
    seed_job(world, doc, state="queued")
    for name, user in world.everyone.items():
        res = world.api.get(ingestion_path(world, doc.id), user)
        assert res.status == 200, (name, res.body)
    res = world.api.get(ingestion_path(world, doc.id), world.plain)
    assert_denied(world.api, res, 404, action="document.read", reason="not_workspace_member")
    res = world.api.get(ingestion_path(world, doc.id), world.t.outsider)
    assert_denied(world.api, res, 404, action="document.read", reason="not_org_member")
    assert world.api.get(ingestion_path(world, doc.id), None).status == 401


def test_grant_holder_without_role_can_read_status(world: World) -> None:
    doc = world.seed()
    seed_job(world, doc, state="queued")
    world.grant(doc.id, world.plain, "read")
    assert world.api.get(ingestion_path(world, doc.id), world.plain).status == 200
    other = world.seed()
    seed_job(world, other, state="queued")
    assert world.api.get(ingestion_path(world, other.id), world.plain).status == 404


def test_foreign_and_unknown_documents_are_404(world: World) -> None:
    mine = world.seed()
    seed_job(world, mine)
    foreign = world.seed_foreign()
    seed_job(world, foreign, org_id=world.foreign.org_id, workspace_id=world.foreign_ws)
    assert world.api.get(ingestion_path(world, foreign.id), world.t.owner).status == 404
    assert world.api.get(ingestion_path(world, uuid.uuid4()), world.t.owner).status == 404
    # The foreign owner asks through their own org/workspace for our document id.
    path = (
        f"/v1/orgs/{world.foreign.org_id}/workspaces/{world.foreign_ws}"
        f"/documents/{mine.id}/ingestion"
    )
    assert world.api.get(path, world.foreign.owner).status == 404
    # ... or through our org path.
    assert world.api.get(ingestion_path(world, mine.id), world.foreign.owner).status == 404


def test_cross_workspace_document_is_404(world: World) -> None:
    other_ws = world.api.create_workspace(world.org_id, world.t.owner)
    source = ensure_source(world.engine, world.org_id, other_ws, world.t.owner.id)
    doc = seed_document(
        world.engine,
        org_id=world.org_id,
        workspace_id=other_ws,
        source_id=source,
        created_by=world.t.owner.id,
    )
    seed_job(world, doc, workspace_id=other_ws)
    # Same org, but not the workspace in the path.
    assert world.api.get(ingestion_path(world, doc.id), world.t.owner).status == 404
    assert (
        world.api.post(ingestion_path(world, doc.id, "/retry"), world.t.owner, None).status == 404
    )


def test_logical_delete_hides_ingestion(world: World) -> None:
    doc = world.seed()
    seed_job(world, doc, state="dead", error_code="pdf_unreadable")
    assert world.api.get(ingestion_path(world, doc.id), world.t.owner).status == 200
    deleted = world.api.delete(world.doc_path(doc.id), world.t.owner)
    assert deleted.status == 202, deleted.body
    for user in (world.t.owner, world.editor, world.viewer):
        assert world.api.get(ingestion_path(world, doc.id), user).status == 404
    assert (
        world.api.post(ingestion_path(world, doc.id, "/retry"), world.t.owner, None).status == 404
    )


# --- retry --------------------------------------------------------------------------------------


def test_retry_role_matrix(world: World) -> None:
    doc = world.seed()
    seed_job(world, doc, state="dead", error_code="timeout")
    res = world.api.post(ingestion_path(world, doc.id, "/retry"), world.viewer, None)
    assert_denied(world.api, res, 403, action="document.write", reason="insufficient_role")
    res = world.api.post(ingestion_path(world, doc.id, "/retry"), world.plain, None)
    assert_denied(world.api, res, 404, action="document.write", reason="not_workspace_member")
    res = world.api.post(ingestion_path(world, doc.id, "/retry"), world.t.outsider, None)
    assert_denied(world.api, res, 404, action="document.write", reason="not_org_member")
    assert world.api.post(ingestion_path(world, doc.id, "/retry"), None, None).status == 401
    # Nothing was changed by the denials.
    assert world.api.get(ingestion_path(world, doc.id), world.viewer).body["state"] == "failed"


def test_retry_dead_job_requeues_atomically(world: World) -> None:
    doc = world.seed()
    job = seed_job(
        world,
        doc,
        state="dead",
        attempts=5,
        stage="index",
        error_code="index_unavailable",
        epoch=7,
        revision_state="failed",
        events=(("queued", None, 0), ("dead", None, 5)),
    )
    res = world.api.post(ingestion_path(world, doc.id, "/retry"), world.editor, None)
    assert res.status == 202, res.body
    assert set(res.body) == {"job_id", "ingestion"}
    assert res.body["job_id"] == str(job.job_id)
    assert res.body["ingestion"]["state"] == "queued"
    assert res.body["ingestion"]["can_retry"] is False
    assert_no_internals(res.body)

    row = job_row(world, job.job_id)
    assert row.state == "queued"
    assert row.attempts == 0
    assert row.last_error_code is None
    assert row.last_error_detail is None
    assert row.finished_at is None
    assert row.lease_owner is None
    assert row.lease_expires_at is None
    assert row.current_stage is None
    assert row.lease_epoch == 7  # the fencing token never decreases
    assert world.row("SELECT state FROM knowledge_revisions WHERE id = :r", r=job.revision_id)[
        0
    ] == ("building")
    assert outbox_count(world, job.job_id) == 1
    last = world.row(
        "SELECT event_type, attempt, detail FROM ingestion_job_events WHERE job_id = :j"
        " ORDER BY seq DESC LIMIT 1",
        j=job.job_id,
    )
    assert last.event_type == "queued"
    assert last.attempt == 0
    assert last.detail == {"reason": "manual_retry"}

    audit = world.api.audit(res.request_id)
    assert [a["event_type"] for a in audit] == ["document.ingestion.retried"]
    assert audit[0]["actor_user_id"] == world.editor.id
    assert audit[0]["org_id"] == world.org_id
    assert audit[0]["metadata"]["job_id"] == str(job.job_id)
    assert audit[0]["metadata"]["from_state"] == "failed"

    body = world.api.get(ingestion_path(world, doc.id), world.viewer).body
    assert body["state"] == "queued"
    assert body["events"][-1]["type"] == "queued"


def test_retry_then_worker_claim_has_a_higher_epoch(world: World) -> None:
    doc = world.seed()
    job = seed_job(world, doc, state="dead", attempts=5, error_code="timeout", epoch=7)
    assert world.api.post(ingestion_path(world, doc.id, "/retry"), world.editor, None).status == 202
    with world.engine.begin() as conn:
        claim = conn.execute(
            text("SELECT lease_epoch, attempts FROM ingest_claim_job(:j, 'w1', 30)"),
            {"j": job.job_id},
        ).one()
    assert (claim.lease_epoch, claim.attempts) == (8, 1)


@pytest.mark.parametrize(
    ("job_state", "code"),
    [
        ("queued", None),
        ("running", None),
        ("succeeded", None),
        ("cancelled", None),
        ("unsupported", "no_text_extracted"),
    ],
)
def test_retry_of_a_non_retryable_state_is_409(
    world: World, job_state: str, code: str | None
) -> None:
    doc = world.seed()
    job = seed_job(world, doc, state=job_state, error_code=code)
    before = (job_row(world, job.job_id), outbox_count(world, job.job_id))
    res = world.api.post(ingestion_path(world, doc.id, "/retry"), world.editor, None)
    assert (res.status, res.body) == (409, {"detail": "conflict"})
    after = (job_row(world, job.job_id), outbox_count(world, job.job_id))
    assert before == after
    assert world.api.audit(res.request_id) == []


def test_repeated_retry_is_rejected_not_duplicated(world: World) -> None:
    doc = world.seed()
    job = seed_job(world, doc, state="dead", attempts=5, error_code="timeout")
    first = world.api.post(ingestion_path(world, doc.id, "/retry"), world.editor, None)
    second = world.api.post(ingestion_path(world, doc.id, "/retry"), world.editor, None)
    assert (first.status, second.status) == (202, 409)
    assert outbox_count(world, job.job_id) == 1


def test_concurrent_retries_have_a_single_winner(world: World) -> None:
    doc = world.seed()
    job = seed_job(world, doc, state="dead", attempts=5, error_code="timeout")
    statuses: list[int] = []
    barrier = threading.Barrier(4)

    def attempt() -> None:
        barrier.wait()
        statuses.append(
            world.api.post(ingestion_path(world, doc.id, "/retry"), world.editor, None).status
        )

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == [202, 409, 409, 409]
    assert outbox_count(world, job.job_id) == 1


def test_retry_unsupported_type_is_allowed(world: World) -> None:
    doc = world.seed()
    seed_job(world, doc, state="unsupported", error_code="unsupported_type")
    res = world.api.post(ingestion_path(world, doc.id, "/retry"), world.t.admin, None)
    assert res.status == 202, res.body
    assert res.body["ingestion"]["state"] == "queued"


def test_retry_without_a_job_creates_one(world: World) -> None:
    doc = world.seed()
    res = world.api.post(ingestion_path(world, doc.id, "/retry"), world.editor, None)
    assert res.status == 202, res.body
    assert res.body["ingestion"]["state"] == "queued"
    assert outbox_count(world, uuid.UUID(res.body["job_id"])) == 1
    again = world.api.post(ingestion_path(world, doc.id, "/retry"), world.editor, None)
    assert again.status == 409


def test_retry_via_document_grant(world: World) -> None:
    doc = world.seed()
    seed_job(world, doc, state="dead", error_code="timeout")
    world.grant(doc.id, world.plain, "read")
    assert world.api.post(ingestion_path(world, doc.id, "/retry"), world.plain, None).status == 403
    world.grant(doc.id, world.other_plain, "write")
    assert (
        world.api.post(ingestion_path(world, doc.id, "/retry"), world.other_plain, None).status
        == 202
    )


# --- timeline pagination -----------------------------------------------------------------------


def test_large_timeline_is_paginated(world: World) -> None:
    doc = world.seed()
    job = seed_job(world, doc, state="running", attempts=1)
    for i in range(130):
        add_event(world, job, "stage_started", "extract", i)
    tail = world.api.get(ingestion_path(world, doc.id), world.viewer).body
    assert len(tail["events"]) == 50  # the newest 50
    assert tail["next_after"] is None
    assert tail["events"][-1]["attempt"] == 129
    seen: list[int] = []
    after = 0
    while True:
        page = world.api.get(
            ingestion_path(world, doc.id), world.viewer, params={"after": after, "limit": 60}
        ).body
        seen.extend(e["id"] for e in page["events"])
        if page["next_after"] is None:
            break
        after = page["next_after"]
    assert len(seen) == 131  # queued + 130
    assert seen == sorted(seen)
    assert len(set(seen)) == len(seen)
    bad = world.api.get(ingestion_path(world, doc.id), world.viewer, params={"limit": 0})
    assert bad.status == 422
    assert (
        world.api.get(ingestion_path(world, doc.id), world.viewer, params={"limit": 201}).status
        == 422
    )


# --- Document.ingestion ---------------------------------------------------------------------


def test_document_list_and_detail_carry_ingestion(world: World) -> None:
    ready = world.seed(title="Ready")
    failed = world.seed(title="Failed")
    bare = world.seed(title="Bare")
    seed_job(world, ready, state="succeeded", revision_state="published")
    seed_job(world, failed, state="dead", error_code="too_large")
    listing = world.api.get(world.path("/documents"), world.editor)
    assert listing.status == 200
    by_title = {i["title"]: i["ingestion"] for i in listing.body["items"]}
    assert by_title["Ready"]["state"] == "ready"
    assert by_title["Failed"]["state"] == "failed"
    assert by_title["Failed"]["error_code"] == "too_large"
    assert by_title["Failed"]["can_retry"] is True
    assert by_title["Bare"] == {
        "state": "none",
        "stage": None,
        "attempts_label": None,
        "error_code": None,
        "message": None,
        "updated_at": None,
        "can_retry": False,
    }
    for item in listing.body["items"]:
        assert_no_internals(item)
    detail = world.api.get(world.doc_path(failed.id), world.viewer).body
    assert detail["ingestion"]["state"] == "failed"
    assert detail["ingestion"]["can_retry"] is False
    assert bare.id


class _Counter:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        self.statements.append(statement)


def _count_ingestion_queries(world: World, path: str) -> tuple[int, int]:
    counter = _Counter()
    event.listen(EngineClass, "before_cursor_execute", counter)
    try:
        res = world.api.get(path, world.editor)
    finally:
        event.remove(EngineClass, "before_cursor_execute", counter)
    assert res.status == 200, res.body
    touching = [s for s in counter.statements if "ingestion_jobs" in s]
    return len(counter.statements), len(touching)


def test_list_has_no_n_plus_one(world: World) -> None:
    for i in range(2):
        d = world.seed(title=f"small-{i}")
        seed_job(world, d, state="dead", error_code="timeout")
    small = _count_ingestion_queries(world, world.path("/documents") + "?limit=200")
    for i in range(25):
        d = world.seed(title=f"big-{i}")
        seed_job(world, d, state="succeeded", revision_state="published")
    big = _count_ingestion_queries(world, world.path("/documents") + "?limit=200")
    assert big == small  # the same number of statements for 27 documents as for 2
    assert big[1] == 1  # exactly one statement reads ingestion jobs


def test_everything_is_in_the_openapi_schema(world: World) -> None:
    spec = world.api.client.get("/openapi.json").json()
    base = "/v1/orgs/{org_id}/workspaces/{workspace_id}"
    paths = spec["paths"]
    assert "get" in paths[f"{base}/documents/{{document_id}}/ingestion"]
    assert "post" in paths[f"{base}/documents/{{document_id}}/ingestion/retry"]
    assert "get" in paths[f"{base}/documents/{{document_id}}/ingestion/events"]
    assert "get" in paths[f"{base}/ingestion/events"]
    schemas = spec["components"]["schemas"]
    assert "ingestion" in schemas["Document"]["properties"]
    assert {"IngestionSummary", "IngestionStatus", "IngestionRetryAccepted"} <= set(schemas)
    assert set(schemas["IngestionSummary"]["properties"]["state"]["enum"]) == {
        "none",
        "queued",
        "processing",
        "ready",
        "failed",
        "unsupported",
        "no_text",
        "cancelled",
    }


# --- PostgreSQL-level isolation -----------------------------------------------------------------


def test_rls_hides_events_and_requeue_across_orgs(world: World, app_engine: Engine) -> None:
    doc = world.seed()
    job = seed_job(world, doc, state="dead", error_code="timeout")
    foreign_org = world.foreign.org_id
    with app_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(foreign_org)}
        )
        assert conn.execute(text("SELECT count(*) FROM ingestion_job_events")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM ingestion_jobs")).scalar_one() == 0
        rows = conn.execute(text("SELECT * FROM ingest_requeue_job(:j)"), {"j": job.job_id}).all()
        assert rows == []  # guessing the id from another org does nothing
    with app_engine.begin() as conn:  # no org context at all
        rows = conn.execute(text("SELECT * FROM ingest_requeue_job(:j)"), {"j": job.job_id}).all()
        assert rows == []
    assert job_row(world, job.job_id).state == "dead"
    assert outbox_count(world, job.job_id) == 0


def test_requeue_function_privileges(world: World) -> None:
    sql = "SELECT has_function_privilege(:r, 'ingest_requeue_job(uuid)', 'EXECUTE')"
    assert world.row(sql, r="ukos_app")[0] is True
    assert world.row(sql, r="ukos_worker")[0] is False
    assert world.row(sql, r="public")[0] is False
    config = world.row(
        "SELECT prosecdef, proconfig FROM pg_proc WHERE proname = 'ingest_requeue_job'"
    )
    assert config.prosecdef is True
    assert any(c.startswith("search_path=pg_catalog, public, pg_temp") for c in config.proconfig)
