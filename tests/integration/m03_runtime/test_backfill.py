"""The backfill CLI logic: discovery through the admin read, writes through the worker role."""

from __future__ import annotations

from compose_support import ComposeProject
from m03_support import Scope, add_version, rows
from pydantic import SecretStr
from sqlalchemy import Engine, text

from app.workers import backfill
from app.workers.settings import WorkerSettings


def test_backfill_enqueues_latest_versions_once(
    admin_engine: Engine, worker_engine: Engine, stack: ComposeProject, scope: Scope
) -> None:
    v2 = add_version(admin_engine, scope, 2)
    cfg = WorkerSettings(DATABASE_WORKER_URL=SecretStr("postgresql://unused/db"))
    space = cfg.embedding_space_id

    found = backfill.discover(admin_engine, space_id=space)
    mine = [t for t in found if t.document_id == scope.document_id]
    assert [t.version_id for t in mine] == [v2]  # only the latest version
    both = backfill.discover(admin_engine, space_id=space, all_versions=True)
    assert {t.version_id for t in both if t.document_id == scope.document_id} == {
        scope.version_id,
        v2,
    }

    candidates, enqueued = backfill.run(
        cfg,
        dry_run=True,
        all_versions=False,
        limit=10_000,
        admin_engine=admin_engine,
        worker_engine=worker_engine,
    )
    assert candidates >= 1
    assert enqueued == 0
    with admin_engine.connect() as conn:
        assert (
            rows(
                conn,
                "SELECT count(*) AS n FROM ingestion_jobs WHERE document_id=:d",
                d=scope.document_id,
            )[0]["n"]
            == 0
        )

    backfill.run(
        cfg,
        dry_run=False,
        all_versions=False,
        limit=10_000,
        admin_engine=admin_engine,
        worker_engine=worker_engine,
    )
    with admin_engine.connect() as conn:
        jobs = rows(
            conn,
            "SELECT version_id, state FROM ingestion_jobs WHERE document_id=:d",
            d=scope.document_id,
        )
        outbox = rows(
            conn,
            "SELECT count(*) AS n FROM outbox_events o JOIN ingestion_jobs j"
            " ON j.id = o.aggregate_id WHERE j.document_id=:d",
            d=scope.document_id,
        )
    assert [(j["version_id"], j["state"]) for j in jobs] == [(v2, "queued")]
    assert outbox[0]["n"] == 1

    # idempotent: nothing left to do, a second run adds nothing
    assert not [
        t
        for t in backfill.discover(admin_engine, space_id=space)
        if t.document_id == scope.document_id
    ]
    backfill.run(
        cfg,
        dry_run=False,
        all_versions=False,
        limit=10_000,
        admin_engine=admin_engine,
        worker_engine=worker_engine,
    )
    with admin_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM ingestion_jobs WHERE document_id=:d"),
                {"d": scope.document_id},
            ).scalar_one()
            == 1
        )

    # deleted documents are skipped
    other = Scope(scope.tenant, scope.document_id, scope.version_id)
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE documents SET deleted_at=now(), deleted_by=:u WHERE id=:d"),
            {"u": other.owner_id, "d": other.document_id},
        )
    assert not [
        t
        for t in backfill.discover(admin_engine, space_id=space, all_versions=True)
        if t.document_id == scope.document_id
    ]
