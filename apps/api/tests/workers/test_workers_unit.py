"""Unit tests of the worker runtime that need no database, broker or Docker."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine

from app.workers import leases
from app.workers.celery_app import (
    QUEUE_CHAT,
    QUEUE_INGEST,
    QUEUE_MAINTENANCE,
    QUEUE_RESEARCH,
    TASK_DELETE_DOCUMENT,
    TASK_RECONCILE,
    TASK_RUN_JOB,
    make_celery,
)
from app.workers.dispatcher import (
    ROUTES,
    Delivery,
    OutboxRow,
    PoisonEvent,
    health_ok,
    transient_retry_seconds,
)
from app.workers.pipeline_port import (
    ClaimedJob,
    LeaseLost,
    PermanentJobError,
    RetryableJobError,
    WorkerConfigError,
    resolve_factory,
)
from app.workers.settings import WorkerSettings
from app.workers.tasks import validate_runtime

CLAIM = ClaimedJob(
    job_id=uuid.uuid4(),
    org_id=uuid.uuid4(),
    workspace_id=uuid.uuid4(),
    document_id=uuid.uuid4(),
    version_id=uuid.uuid4(),
    revision_id=uuid.uuid4(),
    attempts=1,
    lease_epoch=3,
    lease_owner="w1",
)


def row(
    event_type: str, payload: dict[str, Any], aggregate_id: uuid.UUID | None = None
) -> OutboxRow:
    return OutboxRow(
        id=uuid.uuid4(),
        seq=1,
        org_id=uuid.uuid4(),
        aggregate_type="x",
        aggregate_id=aggregate_id or uuid.uuid4(),
        event_type=event_type,
        payload=payload,
        attempts=0,
    )


# -- backoff -----------------------------------------------------------------------------------


def test_backoff_doubles_per_attempt_without_jitter() -> None:
    values = [leases.backoff_seconds(n, 10, jitter=0) for n in range(1, 6)]
    assert values == [10, 20, 40, 80, 160]


def test_backoff_is_capped_at_one_hour() -> None:
    assert leases.backoff_seconds(30, 10, jitter=0) == 3600
    assert leases.backoff_seconds(1000, 10, jitter=0) == 3600
    # the cap holds even with the maximum positive jitter
    assert leases.backoff_seconds(30, 10, rng=lambda: 1.0) == 3600


def test_backoff_jitter_is_within_twenty_percent() -> None:
    low = leases.backoff_seconds(3, 10, rng=lambda: 0.0)
    high = leases.backoff_seconds(3, 10, rng=lambda: 1.0)
    assert (low, high) == (32, 48)  # 40 +/- 20 %


def test_backoff_zero_base_and_invalid_attempts() -> None:
    assert leases.backoff_seconds(3, 0) == 0
    with pytest.raises(ValueError, match="attempts"):
        leases.backoff_seconds(0, 10)


def test_dispatcher_transient_backoff_is_exponential_and_capped() -> None:
    assert [transient_retry_seconds(n) for n in range(4)] == [2, 4, 8, 16]
    assert transient_retry_seconds(50) == 300


# -- error text hygiene ------------------------------------------------------------------------


def test_error_detail_is_truncated_and_stripped_of_control_characters() -> None:
    detail = leases.clean_detail("a\x00b\nc" + "x" * 1000)
    assert detail is not None
    assert len(detail) == 500
    assert "\x00" not in detail
    assert "\n" not in detail
    assert leases.clean_detail(None) is None


def test_error_codes_are_stable_identifiers() -> None:
    assert leases.clean_code("storage_unavailable") == "storage_unavailable"
    assert leases.clean_code("Has Spaces and 'quotes'") == "invalid_error_code"
    assert leases.clean_code("x" * 100) == "invalid_error_code"


def test_owner_is_unique_per_call() -> None:
    assert leases.new_owner() != leases.new_owner()


# -- settings ----------------------------------------------------------------------------------


def test_visibility_timeout_exceeds_every_task_hard_limit() -> None:
    s = WorkerSettings()
    assert s.ingest_hard_limit_seconds > s.INGEST_TASK_SOFT_LIMIT_SECONDS
    assert s.visibility_timeout_seconds > s.ingest_hard_limit_seconds
    assert s.visibility_timeout_seconds > s.MAINTENANCE_TASK_SOFT_LIMIT_SECONDS + 60


def test_defaults_point_at_the_pipeline_modules() -> None:
    s = WorkerSettings()
    assert s.INGEST_PIPELINE_RUNNER == "app.modules.ingestion.pipeline:build_pipeline_runner"
    assert s.INGEST_DELETE_HANDLER.endswith(":build_document_deleted_handler")
    assert s.INGEST_RECONCILE_HANDLER.endswith(":build_reconcile_handler")
    assert s.OUTBOX_BATCH_SIZE == 50


def test_celery_configuration_is_at_least_once_safe() -> None:
    s = WorkerSettings()
    conf = make_celery(s).conf
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.task_track_started is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.broker_connection_retry_on_startup is True
    assert conf.task_serializer == "json"
    assert list(conf.accept_content) == ["json"]
    assert conf.task_create_missing_queues is False
    assert {q.name for q in conf.task_queues} == {
        QUEUE_INGEST,
        QUEUE_MAINTENANCE,
        QUEUE_CHAT,
        QUEUE_RESEARCH,
    }
    assert conf.broker_transport_options["visibility_timeout"] == s.visibility_timeout_seconds
    assert conf.task_routes[TASK_RUN_JOB]["queue"] == QUEUE_INGEST


def test_task_names_are_stable() -> None:
    from app.workers import tasks

    assert (TASK_RUN_JOB, TASK_RECONCILE, TASK_DELETE_DOCUMENT) == (
        "ingest.run_job",
        "maintenance.reconcile",
        "maintenance.delete_document",
    )
    assert tasks.run_job_task.name == "ingest.run_job"
    assert tasks.run_job_task.max_retries == 0
    assert tasks.run_job_task.soft_time_limit < tasks.run_job_task.time_limit


# -- routing -----------------------------------------------------------------------------------


def test_job_queued_routes_to_the_ingest_queue() -> None:
    job = uuid.uuid4()
    d = ROUTES["ingestion.job.queued"](row("ingestion.job.queued", {"job_id": str(job)}))
    assert d == Delivery(TASK_RUN_JOB, QUEUE_INGEST, (str(job),))


def test_malformed_job_payload_is_poison() -> None:
    with pytest.raises(PoisonEvent):
        ROUTES["ingestion.job.queued"](row("ingestion.job.queued", {}))
    with pytest.raises(PoisonEvent):
        ROUTES["ingestion.job.queued"](row("ingestion.job.queued", {"job_id": "not-a-uuid"}))


def test_document_events_route_to_maintenance() -> None:
    doc = uuid.uuid4()
    r = row("document.deleted", {"document_id": str(doc)})
    d = ROUTES["document.deleted"](r)
    assert d == Delivery(TASK_DELETE_DOCUMENT, QUEUE_MAINTENANCE, (str(r.org_id), str(doc)))
    r2 = row("knowledge.reconcile", {}, aggregate_id=doc)  # falls back to the aggregate id
    assert ROUTES["knowledge.reconcile"](r2).args[1] == str(doc)
    r3 = row("knowledge.revision.published", {"document_id": str(doc)})
    assert ROUTES["knowledge.revision.published"](r3).task == TASK_RECONCILE


# -- dotted-path resolution --------------------------------------------------------------------


def test_resolve_factory_accepts_both_spellings() -> None:
    assert resolve_factory("os.path:join", setting="X") is os.path.join
    assert resolve_factory("os.path.join", setting="X") is os.path.join


def test_resolve_factory_clear_errors() -> None:
    with pytest.raises(WorkerConfigError, match=r"INGEST_PIPELINE_RUNNER.*cannot import"):
        resolve_factory("app.modules.nope:build", setting="INGEST_PIPELINE_RUNNER")
    with pytest.raises(WorkerConfigError, match="no callable"):
        resolve_factory("os.path:nope", setting="X")
    with pytest.raises(WorkerConfigError, match="dotted path"):
        resolve_factory("nodots", setting="X")


def test_validate_runtime_requires_the_worker_url_and_importable_implementations() -> None:
    no_url = WorkerSettings()
    with pytest.raises(WorkerConfigError, match="DATABASE_WORKER_URL"):
        validate_runtime(no_url)
    bad = WorkerSettings(
        DATABASE_WORKER_URL=SecretStr("postgresql://w:w@h/db"),
        INGEST_PIPELINE_RUNNER="app.workers.does_not_exist:build",
    )
    with pytest.raises(WorkerConfigError, match="INGEST_PIPELINE_RUNNER"):
        validate_runtime(bad)


# -- health file -------------------------------------------------------------------------------


def test_health_file_freshness(tmp_path: Path) -> None:
    f = tmp_path / "alive"
    s = WorkerSettings(DISPATCHER_HEALTH_FILE=str(f), DISPATCHER_HEALTH_MAX_AGE_SECONDS=30)
    assert health_ok(s) is False  # missing
    f.touch()
    assert health_ok(s) is True
    assert health_ok(s, now=time.time() + 120) is False  # stale


# -- heartbeat state machine (clock and database faked) ------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make_hb(clock: FakeClock, lease: int = 100) -> leases.Heartbeater:
    engine: Engine = None  # type: ignore[assignment]
    return leases.Heartbeater(engine, CLAIM, lease_seconds=lease, interval_seconds=10, clock=clock)


def test_fence_holds_until_ninety_percent_of_the_lease_without_a_renewal() -> None:
    clock = FakeClock()
    hb = make_hb(clock)
    hb.check()
    clock.t += 89
    hb.check()
    clock.t += 2
    with pytest.raises(LeaseLost):
        hb.check()
    assert hb.lost


def test_successful_beat_extends_the_local_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    hb = make_hb(clock)
    monkeypatch.setattr(leases, "heartbeat_once", lambda *_a, **_k: None)
    clock.t += 80
    hb.beat()
    clock.t += 80  # 160 s since the claim, 80 s since the beat
    hb.check()


def test_rejected_beat_marks_the_lease_lost_for_good(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    hb = make_hb(clock)

    def reject(*_a: object, **_k: object) -> None:
        raise LeaseLost("fenced")

    monkeypatch.setattr(leases, "heartbeat_once", reject)
    with pytest.raises(LeaseLost):
        hb.beat()
    with pytest.raises(LeaseLost):
        hb.check()  # stays lost


def test_error_hierarchy() -> None:
    assert RetryableJobError("x").code == "x"
    assert PermanentJobError("y", "detail").detail == "detail"
    assert not issubclass(LeaseLost, (RetryableJobError, PermanentJobError))
