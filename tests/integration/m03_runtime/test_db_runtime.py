"""Fencing, job outcomes and dispatcher passes against real Postgres (and Redis where noted).

In-process: the worker functions are called directly (threads model zombie workers), so these are
deterministic. The process-level end-to-end tests are in ``test_e2e.py``.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
import redis
from compose_support import ComposeProject
from fake_pipeline import FakeRunner
from m03_support import Scope, insert_job, rows, seed_scope
from pydantic import SecretStr
from runtime_support import (
    broker_url,
    events,
    job,
    new_job,
    outbox_for,
    scenario,
    worker_db_url,
)
from sqlalchemy import Engine, text

from app.workers import leases
from app.workers.celery_app import make_celery
from app.workers.dispatcher import (
    ROUTES,
    BrokerUnavailable,
    Delivery,
    Dispatcher,
    celery_publisher,
)
from app.workers.execution import run_ingest_job
from app.workers.pipeline_port import LeaseLost
from app.workers.settings import WorkerSettings
from app.workers.worker_db import worker_session


def settings(stack: ComposeProject, **kw: Any) -> WorkerSettings:
    base: dict[str, Any] = {
        "DATABASE_WORKER_URL": SecretStr(worker_db_url(stack)),
        "CELERY_BROKER_URL": SecretStr(broker_url(stack)),
        "INGEST_LEASE_SECONDS": 6,
        "INGEST_HEARTBEAT_SECONDS": 2,
        "INGEST_RETRY_BASE_SECONDS": 1,
        "OUTBOX_BATCH_SIZE": 5,
    }
    base.update(kw)
    return WorkerSettings(**base)


def make_queued(admin: Engine, scope: Scope, **kw: Any) -> uuid.UUID:
    with admin.begin() as conn:
        return insert_job(conn, scope, **kw)


# -- claims and fencing ---------------------------------------------------------


def test_claim_is_a_noop_unless_queued_and_due(
    admin_engine: Engine, worker_engine: Engine, scope: Scope
) -> None:
    queued = make_queued(admin_engine, scope)
    assert leases.claim_job(worker_engine, queued, "w1", 30) is not None
    assert leases.claim_job(worker_engine, queued, "w2", 30) is None  # already running
    future = make_queued(admin_engine, scope, run_after_seconds=3600)
    assert leases.claim_job(worker_engine, future, "w1", 30) is None  # not due
    done = make_queued(admin_engine, scope, state="succeeded")
    assert leases.claim_job(worker_engine, done, "w1", 30) is None
    assert leases.claim_job(worker_engine, uuid.uuid4(), "w1", 30) is None  # unknown id


def test_stale_epoch_cannot_write_anything(
    admin_engine: Engine, worker_engine: Engine, scope: Scope
) -> None:
    job_id = make_queued(admin_engine, scope)
    c1 = leases.claim_job(worker_engine, job_id, "w1", 30)
    assert c1 is not None
    assert c1.lease_epoch == 1
    # w1 stalls; its lease lapses; the reaper re-queues; w2 claims.
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE ingestion_jobs SET lease_expires_at = now() - interval '1 second'"
                " WHERE id = :j"
            ),
            {"j": job_id},
        )
    assert leases.reap_expired_leases(worker_engine, 0) >= 1
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE ingestion_jobs SET run_after = now() WHERE id = :j"), {"j": job_id}
        )
    c2 = leases.claim_job(worker_engine, job_id, "w2", 30)
    assert c2 is not None
    assert c2.lease_epoch == 2
    assert c2.attempts == 2
    before = job(admin_engine, job_id)
    before_events = events(admin_engine, job_id)

    # the zombie tries everything
    with pytest.raises(LeaseLost):
        leases.heartbeat_once(worker_engine, c1, 30)
    with pytest.raises(LeaseLost):
        leases.set_stage(worker_engine, c1, "embed")
    with pytest.raises(LeaseLost):
        leases.finish_succeeded(worker_engine, c1)
    with pytest.raises(LeaseLost):
        leases.finish_dead(worker_engine, c1, "x_code", None, reason="permanent")
    with pytest.raises(LeaseLost):
        leases.finish_unsupported(worker_engine, c1, "unsupported_type", None)
    with pytest.raises(LeaseLost):
        leases.schedule_retry(worker_engine, c1, "storage_unavailable", None, base_seconds=1)
    with pytest.raises(LeaseLost), worker_session(worker_engine, c1.org_id) as s:
        leases.job_is_ours(s, c1)

    after = job(admin_engine, job_id)
    assert after == before  # not a single column changed
    assert events(admin_engine, job_id) == before_events
    assert len(outbox_for(admin_engine, job_id)) == 1  # only the reaper's row
    assert after["lease_owner"] == c2.lease_owner
    assert after["lease_epoch"] == 2
    leases.finish_succeeded(worker_engine, c2)  # the rightful owner still can
    assert job(admin_engine, job_id)["state"] == "succeeded"


def test_cancelled_job_rejects_the_running_worker(
    admin_engine: Engine, worker_engine: Engine, scope: Scope
) -> None:
    job_id = make_queued(admin_engine, scope)
    c = leases.claim_job(worker_engine, job_id, "w1", 30)
    assert c is not None
    with admin_engine.begin() as conn:  # what the document-deleted handler does
        conn.execute(
            text(
                "UPDATE ingestion_jobs SET state='cancelled', lease_owner=NULL,"
                " lease_expires_at=NULL WHERE id=:j"
            ),
            {"j": job_id},
        )
    with pytest.raises(LeaseLost):
        leases.heartbeat_once(worker_engine, c, 30)
    with pytest.raises(LeaseLost):
        leases.finish_succeeded(worker_engine, c)
    assert job(admin_engine, job_id)["state"] == "cancelled"


def test_heartbeat_extends_the_lease(
    admin_engine: Engine, worker_engine: Engine, scope: Scope
) -> None:
    job_id = make_queued(admin_engine, scope)
    c = leases.claim_job(worker_engine, job_id, "w1", 10)
    assert c is not None
    first = job(admin_engine, job_id)
    leases.heartbeat_once(worker_engine, c, 100)
    second = job(admin_engine, job_id)
    assert second["lease_expires_at"] > first["lease_expires_at"]
    assert second["heartbeat_at"] >= first["heartbeat_at"]


def test_zombie_thread_after_lease_loss_writes_nothing(
    admin_engine: Engine, worker_engine: Engine, stack: ComposeProject, scope: Scope, fake_dir: Path
) -> None:
    """A real zombie: worker 1 is stuck inside its pipeline while its lease expires, the job is
    re-queued and finished by worker 2; when worker 1 wakes it is fenced out."""
    cfg = settings(stack, INGEST_LEASE_SECONDS=600, INGEST_HEARTBEAT_SECONDS=300)
    job_id = make_queued(admin_engine, scope)
    scenario(fake_dir, job_id, {"1": {"mode": "block_unaware"}, "default": {"mode": "succeed"}})
    runner = FakeRunner(fake_dir)
    result: dict[str, str] = {}

    def zombie() -> None:
        result["outcome"] = run_ingest_job(
            job_id, engine=worker_engine, settings=cfg, runner=runner, owner="zombie"
        )

    t = threading.Thread(target=zombie)
    t.start()
    from compose_support import wait_until

    wait_until(lambda: (fake_dir / f"{job_id}.blocked").exists(), timeout=20, description="block")
    with admin_engine.begin() as conn:  # the zombie's heartbeats are not simulated away: expire it
        conn.execute(
            text(
                "UPDATE ingestion_jobs SET lease_expires_at = now() - interval '1 s' WHERE id = :j"
            ),
            {"j": job_id},
        )
    assert leases.reap_expired_leases(worker_engine, 0) == 1
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE ingestion_jobs SET run_after = now() WHERE id=:j"), {"j": job_id})
    assert (
        run_ingest_job(job_id, engine=worker_engine, settings=cfg, runner=runner, owner="w2")
        == "succeeded"
    )
    (fake_dir / f"{job_id}.release.1").write_text("go")  # wake the zombie
    t.join(30)
    assert result["outcome"] == "lease_lost"
    assert not (fake_dir / f"{job_id}.zombie_write").exists()  # its stage write was rejected
    final = job(admin_engine, job_id)
    assert (final["state"], final["lease_epoch"], final["attempts"]) == ("succeeded", 2, 2)
    ev = events(admin_engine, job_id)
    assert ev.count("succeeded") == 1
    assert ev.count("started") == 2


# -- outcomes ------------------------------------------------------------


def run(
    admin: Engine,
    worker: Engine,
    cfg: WorkerSettings,
    fake_dir: Path,
    scope: Scope,
    spec: dict[str, Any],
    **job_kw: Any,
) -> tuple[uuid.UUID, str]:
    job_id = make_queued(admin, scope, **job_kw)
    scenario(fake_dir, job_id, spec)
    outcome = run_ingest_job(job_id, engine=worker, settings=cfg, runner=FakeRunner(fake_dir))
    return job_id, outcome


def test_outcome_mapping(
    admin_engine: Engine, worker_engine: Engine, stack: ComposeProject, scope: Scope, fake_dir: Path
) -> None:
    cfg = settings(stack)

    def go(spec: dict[str, Any], **kw: Any) -> tuple[dict[str, Any], str, list[str]]:
        jid, outcome = run(admin_engine, worker_engine, cfg, fake_dir, scope, spec, **kw)
        return job(admin_engine, jid), outcome, events(admin_engine, jid)

    row, outcome, ev = go({"default": {"mode": "succeed"}})
    assert (outcome, row["state"], row["lease_owner"], row["current_stage"]) == (
        "succeeded",
        "succeeded",
        None,
        None,
    )
    assert row["finished_at"] is not None
    assert ev == ["started", "succeeded"]

    row, outcome, ev = go({"default": {"mode": "unsupported", "code": "unsupported_type"}})
    assert (outcome, row["state"], row["last_error_code"]) == (
        "unsupported",
        "unsupported",
        "unsupported_type",
    )

    row, outcome, _ = go({"default": {"mode": "no_text"}})
    assert (row["state"], row["last_error_code"]) == ("unsupported", "no_text_extracted")

    row, outcome, ev = go({"default": {"mode": "permanent", "code": "pdf_unreadable"}})
    assert (outcome, row["state"], row["attempts"], row["last_error_code"]) == (
        "dead",
        "dead",
        1,
        "pdf_unreadable",
    )
    assert ev == ["started", "dead"]

    row, outcome, ev = go({"default": {"mode": "retryable"}})
    assert (outcome, row["state"], row["last_error_code"]) == (
        "retry_scheduled",
        "queued",
        "storage_unavailable",
    )
    assert row["lease_owner"] is None
    assert row["run_after"] is not None
    assert ev == ["started", "retry_scheduled"]
    assert len(outbox_for(admin_engine, row["id"])) == 1  # the retry's own wake-up row

    # unexpected exception: retried as internal_error, the message (document content) never stored
    row, outcome, _ = go({"default": {"mode": "explode"}})
    assert (outcome, row["last_error_code"], row["last_error_detail"]) == (
        "retry_scheduled",
        "internal_error",
        "ValueError",
    )

    # exhausted attempts -> dead, keeps the last error code
    row, outcome, ev = go({"default": {"mode": "retryable"}}, attempts=1, max_attempts=2)
    assert (outcome, row["state"], row["last_error_code"]) == (
        "dead",
        "dead",
        "storage_unavailable",
    )
    assert ev[-1] == "dead"


def test_unaware_runner_that_returns_success_after_fencing_is_rejected(
    admin_engine: Engine, worker_engine: Engine, stack: ComposeProject, scope: Scope
) -> None:
    cfg = settings(stack)
    job_id = make_queued(admin_engine, scope)

    class Steals:
        def run(self, claim: Any, ctx: Any) -> Any:
            with admin_engine.begin() as conn:  # someone else takes the job meanwhile
                conn.execute(
                    text(
                        "UPDATE ingestion_jobs SET lease_owner='other', lease_epoch=lease_epoch+1"
                        " WHERE id=:j"
                    ),
                    {"j": claim.job_id},
                )
            from app.workers.pipeline_port import PipelineResult

            return PipelineResult("succeeded")

    out = run_ingest_job(job_id, engine=worker_engine, settings=cfg, runner=Steals())
    assert out == "lease_lost"
    row = job(admin_engine, job_id)
    assert (row["state"], row["lease_owner"]) == ("running", "other")
    assert "succeeded" not in events(admin_engine, job_id)


# -- dispatcher passes -------------------------------------------------------------


def outbox_job(
    admin: Engine,
    scope: Scope,
    payload: dict[str, Any] | None = None,
    event_type: str = "ingestion.job.queued",
) -> tuple[uuid.UUID, uuid.UUID]:
    job_id = make_queued(admin, scope)
    event_id = uuid.uuid4()
    import json

    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO outbox_events"
                " (id, org_id, aggregate_type, aggregate_id, event_type, payload)"
                " VALUES (:i, :o, 'ingestion_job', :a, :t, CAST(:p AS jsonb))"
            ),
            {
                "i": event_id,
                "o": scope.org_id,
                "a": job_id,
                "t": event_type,
                "p": json.dumps(payload if payload is not None else {"job_id": str(job_id)}),
            },
        )
    return job_id, event_id


def outbox(admin: Engine, event_id: uuid.UUID) -> dict[str, Any]:
    with admin.connect() as conn:
        return rows(conn, "SELECT * FROM outbox_events WHERE id = :i", i=event_id)[0]


def test_dispatch_publishes_in_seq_order_and_marks_dispatched(
    admin_engine: Engine, worker_engine: Engine, stack: ComposeProject
) -> None:
    sc = seed_scope(admin_engine)
    made = [outbox_job(admin_engine, sc) for _ in range(3)]
    sent: list[Delivery] = []
    d = Dispatcher(worker_engine, settings(stack), sent.append)
    # other tests may leave undispatched rows; drain, then check ours are in order
    while d.dispatch_once():
        pass
    mine = [x.args[0] for x in sent if x.args[0] in {str(j) for j, _ in made}]
    assert mine == [str(j) for j, _ in made]
    assert all(outbox(admin_engine, e)["dispatched_at"] is not None for _, e in made)


def test_unknown_and_malformed_events_are_quarantined_not_looped(
    admin_engine: Engine, worker_engine: Engine, stack: ComposeProject
) -> None:
    sc = seed_scope(admin_engine)
    _, good = outbox_job(admin_engine, sc)
    _, malformed = outbox_job(admin_engine, sc, payload={})
    _, unrouted = outbox_job(admin_engine, sc, event_type="knowledge.reconcile")
    cfg = settings(stack)
    routes = {k: v for k, v in ROUTES.items() if k != "knowledge.reconcile"}
    sent: list[Delivery] = []
    d = Dispatcher(worker_engine, cfg, sent.append, routes=routes)
    while d.dispatch_once():
        pass
    assert outbox(admin_engine, good)["dispatched_at"] is not None
    for bad, reason in ((malformed, "malformed job_id"), (unrouted, "unknown_event_type")):
        row = outbox(admin_engine, bad)
        assert row["dispatched_at"] is None
        assert row["attempts"] == 1
        assert row["last_error"] == f"poison:{reason}"
        assert (row["available_at"] - row["created_at"]).total_seconds() > 3000  # pushed away
    n = len(sent)
    assert d.dispatch_once() == 0
    assert len(sent) == n


def test_broker_outage_rolls_back_and_loses_nothing(
    admin_engine: Engine, worker_engine: Engine, stack: ComposeProject
) -> None:
    sc = seed_scope(admin_engine)
    _, e1 = outbox_job(admin_engine, sc)
    state = {"down": True}
    sent: list[Delivery] = []

    def publish(delivery: Delivery) -> None:
        if state["down"]:
            raise BrokerUnavailable("OperationalError")
        sent.append(delivery)

    d = Dispatcher(worker_engine, settings(stack), publish)
    with pytest.raises(BrokerUnavailable):
        d.dispatch_once()
    row = outbox(admin_engine, e1)
    assert row["dispatched_at"] is None
    assert row["attempts"] == 0
    state["down"] = False
    while d.dispatch_once():
        pass
    assert outbox(admin_engine, e1)["dispatched_at"] is not None


def test_crash_between_publish_and_mark_redelivers_harmlessly(
    admin_engine: Engine,
    worker_engine: Engine,
    stack: ComposeProject,
    scope: Scope,
    app_engine: Engine,
    redis_client: redis.Redis,
    clean_broker: None,
    fake_dir: Path,
) -> None:
    cfg = settings(stack)
    job_id = new_job(app_engine, admin_engine, scope)
    (e1,) = [r["id"] for r in outbox_for(admin_engine, job_id)]
    app = make_celery(cfg)
    real = celery_publisher(app)

    class Crash(BaseException):
        pass

    def publish_then_die(delivery: Delivery) -> None:
        real(delivery)
        raise Crash

    d = Dispatcher(worker_engine, cfg, publish_then_die)
    with pytest.raises(Crash):
        d.dispatch_once()
    assert outbox(admin_engine, e1)["dispatched_at"] is None  # rolled back with the crash
    assert redis_client.llen("ingest") == 1  # ...but the message did go out
    Dispatcher(worker_engine, cfg, real).dispatch_once()
    assert outbox(admin_engine, e1)["dispatched_at"] is not None
    assert redis_client.llen("ingest") == 2  # duplicate delivery
    # both deliveries are harmless: the first claims, the second claims nothing
    runner = FakeRunner(fake_dir)
    outcomes = [
        run_ingest_job(job_id, engine=worker_engine, settings=cfg, runner=runner) for _ in range(2)
    ]
    assert outcomes == ["succeeded", "skipped"]
    assert job(admin_engine, job_id)["attempts"] == 1


def test_two_dispatchers_publish_each_row_exactly_once(
    admin_engine: Engine, worker_engine: Engine, stack: ComposeProject
) -> None:
    sc = seed_scope(admin_engine)
    made = {str(outbox_job(admin_engine, sc)[0]) for _ in range(24)}
    lock = threading.Lock()
    sent: list[str] = []

    def publish(delivery: Delivery) -> None:
        with lock:
            sent.append(delivery.args[0])

    def drain() -> None:
        d = Dispatcher(worker_engine, settings(stack), publish)
        while d.dispatch_once():
            pass

    threads = [threading.Thread(target=drain) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    mine = [s for s in sent if s in made]
    assert sorted(mine) == sorted(made)  # all, none twice


def test_reconcile_sweep_queues_due_publications_and_marks_them(
    admin_engine: Engine, worker_engine: Engine, stack: ComposeProject
) -> None:
    from m03_support import insert_publication, insert_revision

    sc = seed_scope(admin_engine)
    with admin_engine.begin() as conn:
        rev = insert_revision(conn, sc, state="published")
        insert_publication(conn, sc, rev)
    sent: list[Delivery] = []
    d = Dispatcher(worker_engine, settings(stack), sent.append)
    d.reconcile_sweep()
    assert (
        Delivery("maintenance.reconcile", "maintenance", (str(sc.org_id), str(sc.document_id)))
        in sent
    )
    with admin_engine.connect() as conn:
        assert (
            rows(
                conn,
                "SELECT reconciled_at FROM document_publications WHERE document_id=:d",
                d=sc.document_id,
            )[0]["reconciled_at"]
            is not None
        )
