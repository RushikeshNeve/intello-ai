"""The ``research.run`` Celery task (M07.02; contract m07 section 4; pattern of ``chat_tasks``).

``research.run(session_id)`` claims the session in PostgreSQL (``research_claim_session``: planned
-> running, lease, fencing epoch), keeps the lease alive from a heartbeat thread and lets
:class:`~app.modules.research.runtime.ResearchExecutor` do the work. Redis is only a broker: a
duplicate or stale delivery claims nothing and is acknowledged quietly; a worker that dies is
recovered by the dispatcher's sweep (``research_reap_expired``: back to ``planned`` with a fresh
outbox row while attempts remain, so the next worker resumes at the first step that is not done).

The model/index dependencies are the chat runtime's (``CHAT_RUNTIME_FACTORY``): one set of
providers, one policy choke point. A factory that cannot build them ends the session
``failed search_unavailable``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Literal

from sqlalchemy import Engine

from app.core.config import Settings
from app.modules.ai.chat import ERR_SEARCH_UNAVAILABLE, ChatDeps
from app.modules.research import repo
from app.modules.research.runtime import (
    Outcome,
    ResearchExecutor,
    SessionHeartbeater,
)
from app.workers import leases
from app.workers.celery_app import TASK_RESEARCH_RUN, celery_app
from app.workers.chat_tasks import get_chat_runtime
from app.workers.settings import get_worker_settings
from app.workers.worker_db import system_connection

log = logging.getLogger(__name__)

ResearchTaskOutcome = Literal["skipped", "invalid"] | Outcome
_SETTINGS = get_worker_settings()
HEARTBEAT_SECONDS = 15.0


def _fail_without_deps(engine: Engine, claim: repo.ClaimedSession) -> Outcome:
    from app.modules.ai import chat_repo

    try:
        with chat_repo.worker_chat_session(
            engine, org_id=claim.org_id, user_id=claim.created_by
        ) as session:
            repo.finish_session(
                session, claim.fence, state="failed", error_code=ERR_SEARCH_UNAVAILABLE
            )
    except repo.SessionFenceLost:
        return "fence_lost"
    return "failed"


def run_research(
    session_id: uuid.UUID,
    *,
    engine: Engine,
    settings: Settings,
    deps_provider: Callable[[], ChatDeps],
    owner: str | None = None,
) -> Literal["skipped"] | Outcome:
    """Claim ``session_id`` and drive it to a final state. Never raises for session failures."""
    owner = owner or leases.new_owner()
    with system_connection(engine) as conn:
        claim = repo.claim_session(conn, session_id, owner, settings.RESEARCH_LEASE_SECONDS)
    if claim is None:
        log.info("research session=%s: nothing to claim; acking", session_id)
        return "skipped"
    log.info("research session=%s attempt=%s claimed", session_id, claim.attempts)
    heartbeater = SessionHeartbeater(
        engine,
        claim,
        lease_seconds=settings.RESEARCH_LEASE_SECONDS,
        interval_seconds=min(HEARTBEAT_SECONDS, settings.RESEARCH_LEASE_SECONDS / 2),
    ).start()
    try:
        try:
            deps = deps_provider()
        except Exception as exc:  # noqa: BLE001 - nothing of the message is kept
            log.error(
                "research session=%s: dependencies unavailable (%s)", session_id, type(exc).__name__
            )
            return _fail_without_deps(engine, claim)
        return ResearchExecutor(
            engine=engine, settings=settings, deps=deps, claim=claim, check=heartbeater.check
        ).run()
    finally:
        heartbeater.stop()


def research_run(session_id: str) -> str:
    try:
        parsed = uuid.UUID(session_id)
    except ValueError:
        log.error("research.run: invalid session id; dropping the message")
        return "invalid"
    runtime = get_chat_runtime()
    return run_research(
        parsed, engine=runtime.engine, settings=runtime.settings, deps_provider=runtime.deps
    )


research_run_task = celery_app.task(
    name=TASK_RESEARCH_RUN,
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
    max_retries=0,  # a retry is database state (reaper + outbox), never Celery's
    soft_time_limit=_SETTINGS.research_soft_limit_seconds,
    time_limit=_SETTINGS.research_hard_limit_seconds,
)(research_run)
