"""End to end with real processes: outbox -> dispatcher -> Redis -> Celery worker -> fake pipeline.

Every test that starts workers owns its processes (the queues are shared, so two tests must never
run workers at once) and flushes the broker first.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from compose_support import ComposeProject, wait_until
from m03_support import Scope, insert_publication, insert_revision, rows, seed_scope
from runtime_support import (
    API_DIR,
    Proc,
    broker_url,
    events,
    job,
    new_job,
    outbox_for,
    runs,
    runtime_env,
    scenario,
    start_dispatcher,
    start_worker,
    wait_state,
)
from sqlalchemy import Engine

from app.core.db import tenant_session
from app.modules.ingestion.jobs import enqueue_document_deleted
from app.workers.celery_app import TASK_RUN_JOB, make_celery
from app.workers.settings import WorkerSettings

pytestmark = pytest.mark.usefixtures("clean_broker")


@pytest.fixture
def env(stack: ComposeProject, fake_dir: Path) -> dict[str, str]:
    return runtime_env(stack, fake_dir)


@pytest.fixture
def procs() -> Iterator[list[Proc]]:
    started: list[Proc] = []
    yield started
    for p in reversed(started):
        p.stop()


def boot(
    procs: list[Proc],
    env: dict[str, str],
    fake_dir: Path,
    *,
    concurrency: int = 2,
    dispatchers: int = 1,
) -> Proc:
    for i in range(dispatchers):
        procs.append(start_dispatcher(env, fake_dir, f"d{i}"))
    worker = start_worker(env, fake_dir, concurrency=concurrency)
    procs.append(worker)
    return worker


def test_outbox_to_succeeded_end_to_end(
    admin_engine: Engine,
    app_engine: Engine,
    scope: Scope,
    env: dict[str, str],
    fake_dir: Path,
    procs: list[Proc],
) -> None:
    job_id = new_job(app_engine, admin_engine, scope)
    boot(procs, env, fake_dir)
    row = wait_state(admin_engine, job_id, {"succeeded"})
    assert (row["attempts"], row["lease_epoch"], row["lease_owner"]) == (1, 1, None)
    assert events(admin_engine, job_id) == ["queued", "started", "succeeded"]
    (ob,) = outbox_for(admin_engine, job_id)
    assert ob["dispatched_at"] is not None  # acknowledged logically once
    assert len(runs(fake_dir, job_id)) == 1


def test_scenarios_retry_exhaustion_permanent_unsupported(
    admin_engine: Engine,
    app_engine: Engine,
    scope: Scope,
    env: dict[str, str],
    fake_dir: Path,
    procs: list[Proc],
) -> None:
    retry_ok = new_job(app_engine, admin_engine, scope)
    scenario(fake_dir, retry_ok, {"1": {"mode": "retryable"}, "default": {"mode": "succeed"}})
    exhausted = new_job(app_engine, admin_engine, scope, max_attempts=2)
    scenario(
        fake_dir, exhausted, {"default": {"mode": "retryable", "code": "embedding_unavailable"}}
    )
    permanent = new_job(app_engine, admin_engine, scope)
    scenario(fake_dir, permanent, {"default": {"mode": "permanent", "code": "pdf_unreadable"}})
    unsupported = new_job(app_engine, admin_engine, scope)
    scenario(fake_dir, unsupported, {"default": {"mode": "unsupported"}})
    boot(procs, env, fake_dir)

    row = wait_state(admin_engine, retry_ok, {"succeeded"})
    assert row["attempts"] == 2
    assert events(admin_engine, retry_ok) == [
        "queued",
        "started",
        "retry_scheduled",
        "started",
        "succeeded",
    ]
    assert len(outbox_for(admin_engine, retry_ok)) == 2
    assert all(o["dispatched_at"] for o in outbox_for(admin_engine, retry_ok))

    row = wait_state(admin_engine, exhausted, {"dead"})
    assert (row["attempts"], row["last_error_code"]) == (2, "embedding_unavailable")
    assert len(runs(fake_dir, exhausted)) == 2

    row = wait_state(admin_engine, permanent, {"dead"})
    assert (row["attempts"], row["last_error_code"]) == (1, "pdf_unreadable")
    assert len(runs(fake_dir, permanent)) == 1  # no retries

    row = wait_state(admin_engine, unsupported, {"unsupported"})
    assert row["last_error_code"] == "unsupported_type"


def test_duplicate_delivery_executes_once(
    admin_engine: Engine,
    app_engine: Engine,
    scope: Scope,
    env: dict[str, str],
    fake_dir: Path,
    procs: list[Proc],
    stack: ComposeProject,
) -> None:
    job_id = new_job(app_engine, admin_engine, scope)
    scenario(fake_dir, job_id, {"default": {"mode": "long", "seconds": 3}})
    # no dispatcher: we deliver by hand, three times while it runs and once after
    procs.append(start_worker(env, fake_dir, concurrency=3))
    client = make_celery(WorkerSettings(CELERY_BROKER_URL=broker_url(stack)))  # type: ignore[arg-type]
    for _ in range(3):
        client.send_task(TASK_RUN_JOB, args=[str(job_id)], queue="ingest")
    wait_state(admin_engine, job_id, {"succeeded"})
    client.send_task(TASK_RUN_JOB, args=[str(job_id)], queue="ingest")
    time.sleep(2)
    assert len(runs(fake_dir, job_id)) == 1
    ev = events(admin_engine, job_id)
    assert ev.count("started") == 1
    assert ev.count("succeeded") == 1
    assert job(admin_engine, job_id)["attempts"] == 1


def test_sigkill_mid_job_lease_expires_reaper_requeues_second_worker_completes(
    admin_engine: Engine,
    app_engine: Engine,
    scope: Scope,
    env: dict[str, str],
    fake_dir: Path,
    procs: list[Proc],
) -> None:
    job_id = new_job(app_engine, admin_engine, scope)
    scenario(fake_dir, job_id, {"1": {"mode": "block"}, "default": {"mode": "succeed"}})
    procs.append(start_dispatcher(env, fake_dir))
    w1 = start_worker(env, fake_dir)
    procs.append(w1)
    wait_until(lambda: (fake_dir / f"{job_id}.blocked").exists(), timeout=30, description="blocked")
    assert job(admin_engine, job_id)["state"] == "running"
    w1.kill9()  # the whole node dies: no ack, no cleanup
    w2 = start_worker(env, fake_dir)
    procs.append(w2)
    row = wait_state(admin_engine, job_id, {"succeeded"}, timeout=90)
    assert (row["attempts"], row["lease_epoch"]) == (2, 2)
    ev = events(admin_engine, job_id)
    assert ev == ["queued", "started", "retry_scheduled", "started", "succeeded"]
    assert [r[0] for r in runs(fake_dir, job_id)] == ["1", "2"]


def test_sigstop_zombie_resumes_after_lease_loss_and_writes_nothing(
    admin_engine: Engine,
    app_engine: Engine,
    scope: Scope,
    env: dict[str, str],
    fake_dir: Path,
    procs: list[Proc],
) -> None:
    job_id = new_job(app_engine, admin_engine, scope)
    scenario(fake_dir, job_id, {"1": {"mode": "block_unaware"}, "default": {"mode": "succeed"}})
    procs.append(start_dispatcher(env, fake_dir))
    zombie = start_worker(env, fake_dir)
    procs.append(zombie)
    wait_until(lambda: (fake_dir / f"{job_id}.blocked").exists(), timeout=30, description="blocked")
    zombie.signal_group(signal.SIGSTOP)  # frozen: no heartbeat, no progress
    procs.append(start_worker(env, fake_dir))  # the replacement
    row = wait_state(admin_engine, job_id, {"succeeded"}, timeout=90)
    assert (row["attempts"], row["lease_epoch"]) == (2, 2)
    (fake_dir / f"{job_id}.release.1").write_text("go")
    zombie.signal_group(signal.SIGCONT)
    time.sleep(4)  # the zombie runs its stale code
    assert (fake_dir / f"{job_id}.zombie_write").exists() is False
    after = job(admin_engine, job_id)
    assert (after["state"], after["lease_epoch"], after["attempts"]) == ("succeeded", 2, 2)
    ev = events(admin_engine, job_id)
    assert ev.count("succeeded") == 1
    assert ev.count("started") == 2
    assert zombie.p.poll() is None  # alive, just fenced out


def test_heartbeat_keeps_a_long_job_alive_past_its_lease(
    admin_engine: Engine,
    app_engine: Engine,
    scope: Scope,
    env: dict[str, str],
    fake_dir: Path,
    procs: list[Proc],
) -> None:
    job_id = new_job(app_engine, admin_engine, scope)
    scenario(fake_dir, job_id, {"default": {"mode": "long", "seconds": 15}})  # lease is 6 s
    boot(procs, env, fake_dir)  # the dispatcher's reaper runs every second
    started = wait_state(admin_engine, job_id, {"running"})
    first_beat = started["heartbeat_at"]
    wait_state(admin_engine, job_id, {"succeeded"}, timeout=60)
    final = job(admin_engine, job_id)
    assert (final["attempts"], final["lease_epoch"]) == (1, 1)  # never reaped, never re-claimed
    assert "retry_scheduled" not in events(admin_engine, job_id)
    assert final["heartbeat_at"] > first_beat
    assert len(runs(fake_dir, job_id)) == 1


def test_two_dispatchers_each_job_processed_exactly_once(
    admin_engine: Engine,
    app_engine: Engine,
    scope: Scope,
    env: dict[str, str],
    fake_dir: Path,
    procs: list[Proc],
) -> None:
    ids = [new_job(app_engine, admin_engine, scope) for _ in range(12)]
    boot(procs, env, fake_dir, concurrency=3, dispatchers=2)
    for j in ids:
        wait_state(admin_engine, j, {"succeeded"})
    assert [len(runs(fake_dir, j)) for j in ids] == [1] * 12


def test_document_deleted_and_reconcile_events_reach_their_handlers(
    admin_engine: Engine,
    app_engine: Engine,
    scope: Scope,
    env: dict[str, str],
    fake_dir: Path,
    procs: list[Proc],
) -> None:
    with tenant_session(user_id=None, org_id=scope.org_id, engine=app_engine) as session:
        enqueue_document_deleted(
            session,
            org_id=scope.org_id,
            workspace_id=scope.workspace_id,
            document_id=scope.document_id,
        )
    other = seed_scope(admin_engine)  # a publication due for the reconcile sweep
    with admin_engine.begin() as conn:
        insert_publication(conn, other, insert_revision(conn, other, state="published"))
    boot(procs, env, fake_dir)
    deleted = fake_dir / "deleted.log"
    reconciled = fake_dir / "reconciled.log"
    wait_until(lambda: deleted.exists(), timeout=30, description="delete handler")
    assert deleted.read_text().split() == [str(scope.org_id), str(scope.document_id)]
    wait_until(lambda: reconciled.exists(), timeout=30, description="reconcile handler")
    assert f"{other.org_id} {other.document_id}" in reconciled.read_text()
    with admin_engine.connect() as conn:
        assert (
            rows(
                conn,
                "SELECT reconciled_at FROM document_publications WHERE document_id=:d",
                d=other.document_id,
            )[0]["reconciled_at"]
            is not None
        )


def test_dispatcher_sigterm_is_graceful(env: dict[str, str], fake_dir: Path) -> None:
    d = start_dispatcher(env, fake_dir)
    health = subprocess.run(
        [sys.executable, "-m", "app.workers.dispatcher", "--check"],
        cwd=API_DIR,
        env=env,
        check=False,
    )
    assert health.returncode == 0
    d.signal_group(signal.SIGTERM)
    assert d.p.wait(15) == 0
    assert "dispatcher stopped" in d.text()
    os.utime(fake_dir / "dispatcher.alive", (1, 1))  # stale file => unhealthy
    stale = subprocess.run(
        [sys.executable, "-m", "app.workers.dispatcher", "--check"],
        cwd=API_DIR,
        env=env,
        check=False,
    )
    assert stale.returncode == 1


def test_worker_sigterm_finishes_the_running_job(
    admin_engine: Engine,
    app_engine: Engine,
    scope: Scope,
    env: dict[str, str],
    fake_dir: Path,
    procs: list[Proc],
) -> None:
    job_id = new_job(app_engine, admin_engine, scope)
    scenario(fake_dir, job_id, {"default": {"mode": "long", "seconds": 4}})
    d = start_dispatcher(env, fake_dir)
    procs.append(d)
    w = start_worker(env, fake_dir)
    wait_state(admin_engine, job_id, {"running"})
    os.kill(w.p.pid, signal.SIGTERM)  # like `docker stop`: only the main process; warm shutdown
    assert w.p.wait(60) == 0
    row = job(admin_engine, job_id)
    assert row["state"] == "succeeded"
    assert row["attempts"] == 1


def test_worker_refuses_to_start_when_the_pipeline_module_is_missing(
    stack: ComposeProject, fake_dir: Path
) -> None:
    bad = runtime_env(stack, fake_dir, INGEST_PIPELINE_RUNNER="app.modules.nope:build")
    p = Proc(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.workers.celery_app",
            "worker",
            "-Q",
            "ingest",
            "-l",
            "info",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
        ],
        bad,
        fake_dir / "bad.log",
    )
    try:
        code = p.p.wait(60)
    finally:
        p.stop()
    assert code != 0
    assert "INGEST_PIPELINE_RUNNER" in p.text()
    assert "cannot import" in p.text()
