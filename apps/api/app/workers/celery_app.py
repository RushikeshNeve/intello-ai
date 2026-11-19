"""The Celery application (M03.01.03; contract §6, ADR-036).

Redis is a broker only: a lost or duplicated message must never lose or duplicate work, because the
job's truth (state, lease, attempts) lives in PostgreSQL and the task claims it there. Settings that
matter for that guarantee:

* ``task_acks_late`` + ``task_reject_on_worker_lost``: a task is acknowledged after it finished;
  a worker that dies mid-task gets the message back (or, if the whole node is gone, the message sits
  unacked until the visibility timeout; the *lease* expiry + reaper + a fresh outbox row recover the
  job long before that);
* prefetch 1, JSON only, tasks carry ids only, explicit routes,
  no implicit queues, no result backend;
* ``visibility_timeout`` above every task's hard limit, so a running task's message is not
  redelivered while it is still working.

``celery -A app.workers.celery_app worker -Q ingest,maintenance`` starts a worker.
"""

from __future__ import annotations

from typing import Any, Final

from celery import Celery
from kombu.entity import Queue

from app.workers.settings import WorkerSettings, get_worker_settings

QUEUE_INGEST: Final = "ingest"
QUEUE_MAINTENANCE: Final = "maintenance"

TASK_RUN_JOB: Final = "ingest.run_job"
TASK_RECONCILE: Final = "maintenance.reconcile"
TASK_DELETE_DOCUMENT: Final = "maintenance.delete_document"


def _queue(name: str) -> Any:
    return Queue(name)  # type: ignore[no-untyped-call]


def make_celery(settings: WorkerSettings | None = None) -> Celery:
    cfg = settings or get_worker_settings()
    app = Celery("ukos", include=["app.workers.tasks"])
    app.conf.update(
        broker_url=cfg.CELERY_BROKER_URL.get_secret_value(),
        broker_connection_retry_on_startup=True,
        broker_connection_timeout=5,
        broker_transport_options={
            "visibility_timeout": cfg.visibility_timeout_seconds,
            "socket_keepalive": True,
            "socket_connect_timeout": 5,
        },
        # Publishing (dispatcher): a short bounded retry; the dispatcher backs off above that.
        task_publish_retry=True,
        task_publish_retry_policy={
            "max_retries": 2,
            "interval_start": 0,
            "interval_step": 0.2,
            "interval_max": 1.0,
        },
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        task_ignore_result=True,
        result_backend=None,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_track_started=True,
        worker_prefetch_multiplier=1,
        worker_cancel_long_running_tasks_on_connection_loss=True,
        worker_hijack_root_logger=False,
        worker_send_task_events=False,
        task_default_queue=QUEUE_MAINTENANCE,
        task_create_missing_queues=False,
        task_queues=(_queue(QUEUE_INGEST), _queue(QUEUE_MAINTENANCE)),
        task_routes={
            TASK_RUN_JOB: {"queue": QUEUE_INGEST},
            TASK_RECONCILE: {"queue": QUEUE_MAINTENANCE},
            TASK_DELETE_DOCUMENT: {"queue": QUEUE_MAINTENANCE},
        },
        enable_utc=True,
        timezone="UTC",
    )
    return app


celery_app = make_celery()
