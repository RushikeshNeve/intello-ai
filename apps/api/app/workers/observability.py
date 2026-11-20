"""Celery signal hooks for correlation ids, JSON logs and metrics (M08.02.04).

Imported by :mod:`app.workers.tasks`. ``task_prerun`` binds the context carried by the message
headers (or a new trace) around the task body; ``task_postrun`` records the duration. Connecting
to ``setup_logging`` makes Celery skip its own logging setup, so the JSON handler is the only one.
"""

from __future__ import annotations

import logging
import time
from contextlib import ExitStack
from typing import Any

from celery.signals import (
    setup_logging,
    task_postrun,
    task_prerun,
    worker_process_init,
    worker_ready,
)

from app.core import tracing
from app.workers.settings import get_worker_settings

log = logging.getLogger("ukos.worker")

_stacks: dict[str, ExitStack] = {}
_started: dict[str, float] = {}
_settings = get_worker_settings()


@setup_logging.connect  # type: ignore[untyped-decorator]
def _setup_logging(**_: Any) -> None:
    tracing.configure_logging("worker", _settings.UKOS_LOG_LEVEL, _settings.UKOS_LOG_FORMAT)


@worker_ready.connect  # type: ignore[untyped-decorator]
def _on_ready(**_: Any) -> None:
    if _settings.METRICS_PORT:
        tracing.metrics()
    tracing.setup_process(_settings, "worker")


@worker_process_init.connect  # type: ignore[untyped-decorator]
def _on_child_init(**_: Any) -> None:
    tracing.setup_otel(
        "worker",
        enabled=_settings.OTEL_ENABLED,
        endpoint=_settings.OTEL_EXPORTER_OTLP_ENDPOINT,
    )


def _headers_of(task: Any) -> dict[str, Any]:
    request = task.request
    return {key: getattr(request, key, None) for key in ("traceparent", "request_id", "org")}


@task_prerun.connect  # type: ignore[untyped-decorator]
def _prerun(task_id: str, task: Any, args: Any = None, **_: Any) -> None:
    headers = {k: v for k, v in _headers_of(task).items() if v}
    stack = ExitStack()
    stack.enter_context(tracing.bind_from_headers(headers, job_id=str(args[0]) if args else None))
    stack.enter_context(tracing.span(f"task {task.name}", **{"task.name": task.name}))
    _stacks[task_id] = stack
    _started[task_id] = time.perf_counter()
    log.info("task started", extra={"task": task.name})


@task_postrun.connect  # type: ignore[untyped-decorator]
def _postrun(task_id: str, task: Any, state: str | None = None, **_: Any) -> None:
    started = _started.pop(task_id, None)
    elapsed = (time.perf_counter() - started) if started is not None else 0.0
    log.info(
        "task finished",
        extra={"task": task.name, "status": state or "", "duration_ms": round(elapsed * 1000, 1)},
    )
    if started is not None:
        outcome = (state or "unknown").lower()
        tracing.metrics()["task_seconds"].labels(task.name, outcome).observe(elapsed)
    stack = _stacks.pop(task_id, None)
    if stack is not None:
        stack.close()
