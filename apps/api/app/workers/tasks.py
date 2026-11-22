"""Celery tasks (M03.01.03). Task names are stable identifiers; arguments are ids only.

* ``ingest.run_job(job_id)`` claims the job in PostgreSQL and runs it
  (:mod:`app.workers.execution`).
  A duplicate or stale delivery claims nothing and is simply acknowledged.
* ``maintenance.delete_document(org_id, document_id)`` and ``maintenance.reconcile(org_id,
  document_id)`` call the configured handlers. They are idempotent and hold no database state, so a
  failure is retried by Celery with backoff (the reconcile sweeper is the backstop for drift).

The pipeline and the handlers are resolved from settings by dotted path, once per process, the first
time a task needs them; :func:`validate_runtime` resolves the paths at worker start so a missing
implementation is a clear startup error instead of a failing task.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from celery.signals import worker_init
from sqlalchemy import Engine

from app.workers import (
    chat_tasks,  # noqa: F401 - registers the ``chat.run`` task
    observability,  # noqa: F401 - correlation ids, JSON logs, task metrics (Celery signals)
    research_tasks,  # noqa: F401 - registers the ``research.run`` task
)
from app.workers.celery_app import (
    TASK_CACHE_EVICT,
    TASK_CONNECTOR_SYNC,
    TASK_DELETE_DOCUMENT,
    TASK_GRAPH_EXTRACT,
    TASK_RECONCILE,
    TASK_REINDEX_RUN,
    TASK_RUN_JOB,
    celery_app,
)
from app.workers.execution import run_ingest_job
from app.workers.pipeline_port import (
    DocumentDeletedHandler,
    PipelineRunner,
    ReconcileHandler,
    WorkerConfigError,
    resolve_factory,
)
from app.workers.settings import WorkerSettings, get_worker_settings
from app.workers.worker_db import get_worker_engine

log = logging.getLogger(__name__)

_SETTINGS = get_worker_settings()


@dataclass
class Runtime:
    settings: WorkerSettings
    engine: Engine
    runner: PipelineRunner
    delete_handler: DocumentDeletedHandler
    reconcile_handler: ReconcileHandler


_lock = threading.Lock()
_runtime: dict[int, Runtime] = {}
_override: Runtime | None = None


def validate_runtime(settings: WorkerSettings) -> None:
    """Fail fast and clearly if the worker cannot run: no worker DB URL, or an implementation
    (pipeline, handlers) that cannot be imported."""
    if settings.sqlalchemy_worker_url is None:
        raise WorkerConfigError("DATABASE_WORKER_URL is not set: workers connect as ukos_worker")
    resolve_factory(settings.INGEST_PIPELINE_RUNNER, setting="INGEST_PIPELINE_RUNNER")
    resolve_factory(settings.INGEST_DELETE_HANDLER, setting="INGEST_DELETE_HANDLER")
    resolve_factory(settings.INGEST_RECONCILE_HANDLER, setting="INGEST_RECONCILE_HANDLER")
    resolve_factory(settings.CHAT_RUNTIME_FACTORY, setting="CHAT_RUNTIME_FACTORY")


def build_runtime(settings: WorkerSettings) -> Runtime:
    validate_runtime(settings)
    return Runtime(
        settings=settings,
        engine=get_worker_engine(settings),
        runner=resolve_factory(settings.INGEST_PIPELINE_RUNNER, setting="INGEST_PIPELINE_RUNNER")(
            settings
        ),
        delete_handler=resolve_factory(
            settings.INGEST_DELETE_HANDLER, setting="INGEST_DELETE_HANDLER"
        )(settings),
        reconcile_handler=resolve_factory(
            settings.INGEST_RECONCILE_HANDLER, setting="INGEST_RECONCILE_HANDLER"
        )(settings),
    )


def get_runtime() -> Runtime:
    """Per-process runtime (prefork children each build their own: no shared sockets)."""
    if _override is not None:
        return _override
    pid = os.getpid()
    with _lock:
        runtime = _runtime.get(pid)
        if runtime is None:
            runtime = build_runtime(_SETTINGS)
            _runtime[pid] = runtime
        return runtime


def set_runtime_override(runtime: Runtime | None) -> None:
    """Tests only: run tasks against an injected runtime."""
    global _override
    _override = runtime


@worker_init.connect  # type: ignore[untyped-decorator]
def _validate_on_worker_init(sender: Any = None, **_: Any) -> None:
    # Celery's signal dispatch swallows ordinary exceptions, so a bad configuration must end the
    # process explicitly: a worker that cannot run its pipeline must not accept messages.
    # The signal is process-global: ignore workers of other Celery apps (e.g. throwaway test apps).
    app_name = getattr(getattr(sender, "app", None), "main", "ukos")
    if app_name != "ukos":
        return
    try:
        validate_runtime(_SETTINGS)
    except WorkerConfigError as exc:
        log.critical("worker cannot start: %s", exc)
        print(f"worker cannot start: {exc}", file=sys.stderr, flush=True)
        os._exit(78)
    log.info("worker runtime validated (pipeline and handlers resolvable)")


# -- task bodies (plain, typed functions; registered below) ------------------------------------


def run_job(job_id: str) -> str:
    try:
        parsed = uuid.UUID(job_id)
    except ValueError:
        log.error("ingest.run_job: invalid job id; dropping the message")
        return "invalid"
    runtime = get_runtime()
    return run_ingest_job(
        parsed, engine=runtime.engine, settings=runtime.settings, runner=runtime.runner
    )


def delete_document(org_id: str, document_id: str) -> None:
    runtime = get_runtime()
    runtime.delete_handler.handle(uuid.UUID(org_id), uuid.UUID(document_id))
    # M07: memories citing the deleted document are no longer valid (best effort: the sweep and
    # the lazy read-time check are the backstops).
    try:
        from app.modules.memory.invalidation import invalidate_document

        invalidate_document(runtime.engine, uuid.UUID(org_id), uuid.UUID(document_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("memory invalidation after document delete failed (%s)", type(exc).__name__)


_connector_override: Any = None
_connector_deps: dict[int, Any] = {}


def set_connector_deps_override(deps: Any) -> None:
    """Tests only: run ``connectors.sync_source`` against injected ``SyncDeps``."""
    global _connector_override
    _connector_override = deps


def _connector_runtime() -> Any:
    if _connector_override is not None:
        return _connector_override
    from app.modules.connectors.wiring import default_deps

    pid = os.getpid()
    with _lock:
        if pid not in _connector_deps:
            _connector_deps[pid] = default_deps(_SETTINGS)
        return _connector_deps[pid]


def sync_source(org_id: str, source_id: str) -> str:
    """``connectors.sync_source``: run the source's oldest queued sync. A sync that is already
    running raises ``SyncBusy`` so Celery retries it later; every other failure is recorded in
    ``source_syncs`` (never raised: no retry storm)."""
    from app.modules.connectors.sync import run_source_sync

    try:
        org, source = uuid.UUID(org_id), uuid.UUID(source_id)
    except ValueError:
        log.error("connectors.sync_source: invalid ids; dropping the message")
        return "invalid"
    return run_source_sync(_connector_runtime(), org, source)


def reconcile(org_id: str, document_id: str) -> None:
    get_runtime().reconcile_handler.handle(uuid.UUID(org_id), uuid.UUID(document_id))


def graph_extract_revision(org_id: str, document_id: str, revision_id: str) -> str:
    from app.modules.graph.tasks import run_default

    return run_default(org_id, document_id, revision_id)


_maintenance_options: dict[str, Any] = {
    "acks_late": True,
    "reject_on_worker_lost": True,
    "ignore_result": True,
    "autoretry_for": (Exception,),
    "retry_backoff": 2,
    "retry_backoff_max": 300,
    "retry_jitter": True,
    "max_retries": 8,
    "soft_time_limit": _SETTINGS.MAINTENANCE_TASK_SOFT_LIMIT_SECONDS,
    "time_limit": _SETTINGS.MAINTENANCE_TASK_SOFT_LIMIT_SECONDS + 60,
}

run_job_task = celery_app.task(
    name=TASK_RUN_JOB,
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
    max_retries=0,  # retries are database state (queued + run_after + outbox), never Celery's
    soft_time_limit=_SETTINGS.INGEST_TASK_SOFT_LIMIT_SECONDS,
    time_limit=_SETTINGS.ingest_hard_limit_seconds,
)(run_job)
delete_document_task = celery_app.task(name=TASK_DELETE_DOCUMENT, **_maintenance_options)(
    delete_document
)
reconcile_task = celery_app.task(name=TASK_RECONCILE, **_maintenance_options)(reconcile)
sync_source_task = celery_app.task(name=TASK_CONNECTOR_SYNC, **_maintenance_options)(sync_source)
graph_extract_task = celery_app.task(name=TASK_GRAPH_EXTRACT, **_maintenance_options)(
    graph_extract_revision
)


def reindex_run(org_id: str, job_id: str) -> str:
    from app.modules.knowledge.reindex.tasks import run_default

    return run_default(org_id, job_id)


def embedding_cache_evict() -> int:
    from sqlalchemy import text

    from app.modules.ai.embeddings.cache import evict_all
    from app.workers.worker_db import get_worker_engine, session_factory_for, system_connection

    engine = get_worker_engine(get_worker_settings())

    def list_orgs(limit: int) -> list[uuid.UUID]:
        with system_connection(engine) as conn:
            rows = conn.execute(text("SELECT * FROM embedding_cache_orgs(:n)"), {"n": limit})
            return [r[0] for r in rows]

    return evict_all(list_orgs, session_factory_for(engine))


embedding_cache_evict_task = celery_app.task(name=TASK_CACHE_EVICT, **_maintenance_options)(
    embedding_cache_evict
)
reindex_run_task = celery_app.task(name=TASK_REINDEX_RUN, **_maintenance_options)(reindex_run)
