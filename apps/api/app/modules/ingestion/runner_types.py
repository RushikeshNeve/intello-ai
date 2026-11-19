"""Names of the worker/pipeline port (``app/workers/pipeline_port.py``, owned by the runtime).

Re-exported so the ingestion and knowledge modules import one local module. ``WorkerSessions`` is
the type of ``JobContext.session_factory``: ``factory(org_id)`` opens a worker-role transaction
with ``app.current_org_id`` set (commit on success, rollback on error).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy.orm import Session

from app.workers.pipeline_port import (
    ClaimedJob,
    DocumentDeletedHandler,
    JobContext,
    JobError,
    LeaseLost,
    PermanentJobError,
    PipelineResult,
    PipelineRunner,
    ReconcileHandler,
    RetryableJobError,
)

WorkerSessions = Callable[[uuid.UUID], AbstractContextManager[Session]]

__all__ = [
    "ClaimedJob",
    "DocumentDeletedHandler",
    "JobContext",
    "JobError",
    "LeaseLost",
    "PermanentJobError",
    "PipelineResult",
    "PipelineRunner",
    "ReconcileHandler",
    "RetryableJobError",
    "WorkerSessions",
]
