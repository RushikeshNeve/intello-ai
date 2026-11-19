"""The ``chat.run`` Celery task (M04.02.04; contract m04 §8, M03 contract §6 for the pattern).

``chat.run(run_id)`` claims the run in PostgreSQL (``chat_claim_run``: queued -> running, lease,
fencing epoch), keeps the lease alive from a heartbeat thread, and lets
:class:`~app.modules.ai.chat.RunExecutor` do the work. Redis is only a broker: a duplicate or stale
delivery claims nothing and is acknowledged quietly; a worker that dies mid-run is recovered by the
dispatcher's sweep (``chat_reap_expired_runs``): requeued once when no token was streamed yet,
else ``failed lease_expired``.

The run's dependencies (knowledge index, embedder, chat settings, model factory) come from
``CHAT_RUNTIME_FACTORY`` (``module:factory``, called as ``factory(settings) -> ChatDeps``), built
once per process on the first run. A factory that cannot build them ends the run
``failed search_unavailable`` (never a crash loop, never a hung run).
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Engine

from app.core.config import Settings
from app.modules.ai import chat_repo
from app.modules.ai.chat import (
    ERR_SEARCH_UNAVAILABLE,
    ChatDeps,
    RunExecutor,
    RunHeartbeater,
    RunOutcome,
    fail_run,
)
from app.modules.ai.models.settings import ChatSettings
from app.workers import leases
from app.workers.celery_app import TASK_CHAT_RUN, celery_app
from app.workers.pipeline_port import resolve_factory
from app.workers.settings import WorkerSettings, get_worker_settings
from app.workers.worker_db import get_worker_engine, system_connection

log = logging.getLogger(__name__)

ChatTaskOutcome = Literal["skipped", "invalid"] | RunOutcome


def build_default_chat_deps(settings: Settings) -> ChatDeps:
    """Production wiring: the RushDB index, the embedding client (``None`` when it cannot be built:
    keyword-only retrieval) and the gateway's ``get_chat_model`` with settings from the env."""
    from app.modules.ingestion import wiring

    repo = wiring.default_repository()
    try:
        embedder = wiring.default_embedder()
    except Exception:  # noqa: BLE001 - retrieval degrades to keyword, as in the search API
        embedder = None
    del settings
    return ChatDeps(repo=repo, embedder=embedder, chat_settings=ChatSettings())


def run_chat(
    run_id: uuid.UUID,
    *,
    engine: Engine,
    settings: Settings,
    deps_provider: Callable[[], ChatDeps],
    owner: str | None = None,
) -> Literal["skipped"] | RunOutcome:
    """Claim ``run_id`` and drive it to a final state. Never raises for run failures."""
    owner = owner or leases.new_owner()
    with system_connection(engine) as conn:
        claim = chat_repo.claim_run(conn, run_id, owner, settings.CHAT_LEASE_SECONDS)
    if claim is None:
        log.info("chat run=%s: nothing to claim (duplicate or finished); acking", run_id)
        return "skipped"
    log.info("chat run=%s attempt=%s epoch=%s claimed", run_id, claim.attempts, claim.lease_epoch)
    heartbeater = RunHeartbeater(
        engine,
        claim,
        lease_seconds=settings.CHAT_LEASE_SECONDS,
        interval_seconds=settings.CHAT_HEARTBEAT_SECONDS,
    ).start()
    try:
        deps: ChatDeps | None
        try:
            deps = deps_provider()
        except Exception as exc:  # noqa: BLE001 - nothing of the message is kept
            log.error("chat run=%s: dependencies unavailable (%s)", run_id, type(exc).__name__)
            deps = None
        if deps is None:
            return fail_run(engine, claim, ERR_SEARCH_UNAVAILABLE)
        return RunExecutor(
            engine=engine, settings=settings, deps=deps, claim=claim, check=heartbeater.check
        ).run()
    finally:
        heartbeater.stop()


# -- runtime (per process) ----------------------------------------------------------------------


@dataclass
class ChatRuntime:
    settings: WorkerSettings
    engine: Engine
    _deps: ChatDeps | None = None

    def deps(self) -> ChatDeps:
        """Built once per process, lazily (importing the task module needs no index or model)."""
        if self._deps is None:
            factory = resolve_factory(
                self.settings.CHAT_RUNTIME_FACTORY, setting="CHAT_RUNTIME_FACTORY"
            )
            built: ChatDeps = factory(self.settings)
            self._deps = built
        return self._deps


_lock = threading.Lock()
_runtimes: dict[int, ChatRuntime] = {}
_override: ChatRuntime | None = None
_SETTINGS = get_worker_settings()


def get_chat_runtime() -> ChatRuntime:
    if _override is not None:
        return _override
    pid = os.getpid()
    with _lock:
        runtime = _runtimes.get(pid)
        if runtime is None:
            runtime = ChatRuntime(_SETTINGS, get_worker_engine(_SETTINGS))
            _runtimes[pid] = runtime
        return runtime


def set_chat_runtime_override(runtime: ChatRuntime | None) -> None:
    """Tests only."""
    global _override
    _override = runtime


def chat_run(run_id: str) -> str:
    try:
        parsed = uuid.UUID(run_id)
    except ValueError:
        log.error("chat.run: invalid run id; dropping the message")
        return "invalid"
    runtime = get_chat_runtime()
    return run_chat(
        parsed,
        engine=runtime.engine,
        settings=runtime.settings,
        deps_provider=runtime.deps,
    )


chat_run_task = celery_app.task(
    name=TASK_CHAT_RUN,
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
    max_retries=0,  # a retry is database state (reaper + outbox), never Celery's
    soft_time_limit=_SETTINGS.chat_soft_limit_seconds,
    time_limit=_SETTINGS.chat_hard_limit_seconds,
)(chat_run)
