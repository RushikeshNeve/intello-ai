"""Celery entry points of the re-index (maintenance queue). Arguments are ids only; a duplicate or
stale delivery claims nothing (lease in PostgreSQL) and is simply acknowledged."""

from __future__ import annotations

import uuid
from typing import Final

from app.modules.ingestion.runner_types import LeaseLost
from app.modules.knowledge.reindex import service
from app.workers.leases import new_owner

TASK_REINDEX_RUN: Final = "maintenance.reindex_run"


def run_default(org_id: str, job_id: str) -> str:
    from app.modules.ingestion import wiring
    from app.workers.settings import get_worker_settings

    sessions = wiring.default_worker_sessions(get_worker_settings())
    try:
        result = service.run_job(
            sessions,
            wiring.default_repository(),
            wiring.default_artifact_store(),
            wiring.default_embedder(),
            uuid.UUID(org_id),
            uuid.UUID(job_id),
            new_owner(),
        )
    except LeaseLost:
        return "lease_lost"
    return result.state if result is not None else "not_claimable"


def purge_default(org_id: str, job_id: str) -> int:
    from app.modules.ingestion import wiring
    from app.workers.settings import get_worker_settings

    sessions = wiring.default_worker_sessions(get_worker_settings())
    return service.purge_old_space(
        sessions,
        wiring.default_repository(),
        wiring.default_artifact_store(),
        uuid.UUID(org_id),
        uuid.UUID(job_id),
    )
