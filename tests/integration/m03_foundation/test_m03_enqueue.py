"""``enqueue_ingestion`` / ``enqueue_document_deleted``: atomic, idempotent, race-safe (M03.01.01).

Runs as the API role (``ukos_app``) through ``tenant_session``, i.e. with the real RLS context.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from m03_support import SPACE, Scope, new_space, seed_scope
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import tenant_session
from app.modules.ingestion.ids import PIPELINE_VERSION, revision_id_for
from app.modules.ingestion.jobs import enqueue_document_deleted, enqueue_ingestion


@pytest.fixture
def scope(admin_engine: Engine, migrated: None) -> Scope:
    return seed_scope(admin_engine)


def enqueue(session: Session, scope: Scope, **over: Any) -> uuid.UUID:
    kwargs: dict[str, Any] = {
        "org_id": scope.org_id,
        "workspace_id": scope.workspace_id,
        "document_id": scope.document_id,
        "version_id": scope.version_id,
        "embedding_space_id": SPACE,
    }
    kwargs.update(over)
    return enqueue_ingestion(session, **kwargs)


def counts(engine: Engine, scope: Scope) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            "jobs": conn.execute(
                text("SELECT count(*) FROM ingestion_jobs WHERE document_id = :d"),
                {"d": scope.document_id},
            ).scalar_one(),
            "events": conn.execute(
                text("SELECT count(*) FROM ingestion_job_events WHERE document_id = :d"),
                {"d": scope.document_id},
            ).scalar_one(),
            "outbox": conn.execute(
                text(
                    "SELECT count(*) FROM outbox_events WHERE org_id = :o"
                    " AND event_type = 'ingestion.job.queued'"
                ),
                {"o": scope.org_id},
            ).scalar_one(),
        }


def test_enqueue_writes_job_event_and_outbox_row(
    admin_engine: Engine, app_engine: Engine, scope: Scope
) -> None:
    with tenant_session(user_id=scope.owner_id, org_id=scope.org_id, engine=app_engine) as s:
        job_id = enqueue(s, scope)

    with admin_engine.connect() as conn:
        job = (
            conn.execute(text("SELECT * FROM ingestion_jobs WHERE id = :j"), {"j": job_id})
            .mappings()
            .one()
        )
        events = (
            conn.execute(
                text("SELECT * FROM ingestion_job_events WHERE job_id = :j"), {"j": job_id}
            )
            .mappings()
            .all()
        )
        outbox = (
            conn.execute(text("SELECT * FROM outbox_events WHERE aggregate_id = :j"), {"j": job_id})
            .mappings()
            .all()
        )

    assert job["revision_id"] == revision_id_for(scope.version_id, PIPELINE_VERSION, SPACE)
    assert (job["org_id"], job["workspace_id"], job["document_id"], job["version_id"]) == (
        scope.org_id,
        scope.workspace_id,
        scope.document_id,
        scope.version_id,
    )
    assert (job["pipeline_version"], job["embedding_space_id"]) == (PIPELINE_VERSION, SPACE)
    assert (job["state"], job["attempts"], job["lease_epoch"]) == ("queued", 0, 0)
    assert job["max_attempts"] == get_settings().INGEST_MAX_ATTEMPTS == 5
    assert job["lease_owner"] is None
    assert job["run_after"] is not None  # due immediately

    assert [(e["event_type"], e["attempt"], e["org_id"]) for e in events] == [
        ("queued", 0, scope.org_id)
    ]
    assert events[0]["detail"] == {"revision_id": str(job["revision_id"])}

    assert len(outbox) == 1
    row = outbox[0]
    assert (row["event_type"], row["aggregate_type"], row["org_id"]) == (
        "ingestion.job.queued",
        "ingestion_job",
        scope.org_id,
    )
    assert row["payload"] == {"job_id": str(job_id)}
    assert row["dispatched_at"] is None
    assert row["attempts"] == 0


def test_max_attempts_can_be_overridden(
    admin_engine: Engine, app_engine: Engine, scope: Scope
) -> None:
    with tenant_session(user_id=scope.owner_id, org_id=scope.org_id, engine=app_engine) as s:
        job_id = enqueue(s, scope, max_attempts=2)
    with admin_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT max_attempts FROM ingestion_jobs WHERE id = :j"), {"j": job_id}
            ).scalar_one()
            == 2
        )


def test_second_call_in_the_same_transaction_inserts_nothing_new(
    admin_engine: Engine, app_engine: Engine, scope: Scope
) -> None:
    with tenant_session(user_id=scope.owner_id, org_id=scope.org_id, engine=app_engine) as s:
        first = enqueue(s, scope)
        second = enqueue(s, scope)
    assert first == second
    assert counts(admin_engine, scope) == {"jobs": 1, "events": 1, "outbox": 1}


def test_second_call_in_a_later_transaction_returns_the_existing_job(
    admin_engine: Engine, app_engine: Engine, scope: Scope
) -> None:
    with tenant_session(user_id=scope.owner_id, org_id=scope.org_id, engine=app_engine) as s:
        first = enqueue(s, scope)
    with tenant_session(user_id=scope.owner_id, org_id=scope.org_id, engine=app_engine) as s:
        second = enqueue(s, scope)
    assert first == second
    assert counts(admin_engine, scope) == {"jobs": 1, "events": 1, "outbox": 1}


def test_a_new_pipeline_or_embedding_space_is_a_new_revision_and_job(
    admin_engine: Engine, app_engine: Engine, scope: Scope
) -> None:
    with tenant_session(user_id=scope.owner_id, org_id=scope.org_id, engine=app_engine) as s:
        base = enqueue(s, scope)
        other_space = enqueue(s, scope, embedding_space_id=new_space())
        other_pipeline = enqueue(s, scope, pipeline_version="2")
    assert len({base, other_space, other_pipeline}) == 3
    assert counts(admin_engine, scope) == {"jobs": 3, "events": 3, "outbox": 3}
    with admin_engine.connect() as conn:
        revisions = set(
            conn.execute(
                text("SELECT revision_id FROM ingestion_jobs WHERE document_id = :d"),
                {"d": scope.document_id},
            ).scalars()
        )
    assert len(revisions) == 3


def test_rollback_leaves_no_job_event_or_outbox_row(
    admin_engine: Engine, app_engine: Engine, scope: Scope
) -> None:
    with (
        pytest.raises(RuntimeError, match="boom"),
        tenant_session(user_id=scope.owner_id, org_id=scope.org_id, engine=app_engine) as s,
    ):
        enqueue(s, scope)
        raise RuntimeError("boom")
    assert counts(admin_engine, scope) == {"jobs": 0, "events": 0, "outbox": 0}
    # ... and the same call afterwards works (nothing was left behind to conflict).
    with tenant_session(user_id=scope.owner_id, org_id=scope.org_id, engine=app_engine) as s:
        enqueue(s, scope)
    assert counts(admin_engine, scope) == {"jobs": 1, "events": 1, "outbox": 1}


def test_concurrent_enqueues_create_exactly_one_job(
    admin_engine: Engine, app_engine: Engine, scope: Scope
) -> None:
    workers = 8
    barrier = threading.Barrier(workers)

    def run() -> uuid.UUID:
        with tenant_session(user_id=scope.owner_id, org_id=scope.org_id, engine=app_engine) as s:
            barrier.wait(timeout=20)
            return enqueue(s, scope)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        ids = [f.result(timeout=60) for f in [pool.submit(run) for _ in range(workers)]]
    assert len(set(ids)) == 1
    assert counts(admin_engine, scope) == {"jobs": 1, "events": 1, "outbox": 1}


def test_loser_of_a_race_whose_winner_rolls_back_still_enqueues(
    admin_engine: Engine, app_engine: Engine, scope: Scope
) -> None:
    """B blocks on A's uncommitted job (same revision); A rolls back; B then inserts its own."""
    a_inserted = threading.Event()
    release_a = threading.Event()
    b_done = threading.Event()
    result: dict[str, uuid.UUID] = {}

    def txn_a() -> None:
        with (
            pytest.raises(RuntimeError, match="a failed"),
            tenant_session(user_id=scope.owner_id, org_id=scope.org_id, engine=app_engine) as s,
        ):
            enqueue(s, scope)
            a_inserted.set()
            release_a.wait(timeout=30)
            raise RuntimeError("a failed")

    def txn_b() -> None:
        with tenant_session(user_id=scope.owner_id, org_id=scope.org_id, engine=app_engine) as s:
            result["b"] = enqueue(s, scope)
        b_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(txn_a)
        assert a_inserted.wait(timeout=30)
        fb = pool.submit(txn_b)
        assert not b_done.wait(timeout=1.0), "B must block on A's uncommitted unique key"
        release_a.set()
        fa.result(timeout=30)
        fb.result(timeout=30)
    assert counts(admin_engine, scope) == {"jobs": 1, "events": 1, "outbox": 1}
    with admin_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT id FROM ingestion_jobs WHERE document_id = :d"),
                {"d": scope.document_id},
            ).scalar_one()
            == result["b"]
        )


def test_enqueue_cannot_write_another_orgs_rows(
    admin_engine: Engine, app_engine: Engine, scope: Scope
) -> None:
    """RLS WITH CHECK: org A's context cannot enqueue a job for org B's ids."""
    other = seed_scope(admin_engine)
    with (
        pytest.raises(DBAPIError) as exc,
        tenant_session(user_id=scope.owner_id, org_id=scope.org_id, engine=app_engine) as s,
    ):
        enqueue(
            s,
            other,  # org B's org/workspace/document/version ids
        )
    assert "row-level security" in str(exc.value.orig)
    assert counts(admin_engine, other) == {"jobs": 0, "events": 0, "outbox": 0}


def test_enqueue_without_context_is_rejected(
    admin_engine: Engine, app_engine: Engine, scope: Scope
) -> None:
    with (
        pytest.raises(DBAPIError) as exc,
        tenant_session(user_id=None, org_id=None, engine=app_engine) as s,
    ):
        enqueue(s, scope)
    assert "row-level security" in str(exc.value.orig)


def test_document_deleted_event(admin_engine: Engine, app_engine: Engine, scope: Scope) -> None:
    with tenant_session(user_id=scope.owner_id, org_id=scope.org_id, engine=app_engine) as s:
        event_id = enqueue_document_deleted(
            s, org_id=scope.org_id, workspace_id=scope.workspace_id, document_id=scope.document_id
        )
    with admin_engine.connect() as conn:
        row = (
            conn.execute(text("SELECT * FROM outbox_events WHERE id = :i"), {"i": event_id})
            .mappings()
            .one()
        )
    assert (row["event_type"], row["aggregate_type"], row["aggregate_id"], row["org_id"]) == (
        "document.deleted",
        "document",
        scope.document_id,
        scope.org_id,
    )
    assert row["payload"] == {
        "document_id": str(scope.document_id),
        "workspace_id": str(scope.workspace_id),
    }
    assert row["dispatched_at"] is None


def test_document_deleted_event_rolls_back_with_its_transaction(
    admin_engine: Engine, app_engine: Engine, scope: Scope
) -> None:
    with (
        pytest.raises(RuntimeError, match="boom"),
        tenant_session(user_id=scope.owner_id, org_id=scope.org_id, engine=app_engine) as s,
    ):
        enqueue_document_deleted(
            s, org_id=scope.org_id, workspace_id=scope.workspace_id, document_id=scope.document_id
        )
        raise RuntimeError("boom")
    with admin_engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM outbox_events WHERE aggregate_id = :d"),
            {"d": scope.document_id},
        ).scalar_one()
    assert n == 0
