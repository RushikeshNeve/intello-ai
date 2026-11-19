"""Local mirror of the worker/pipeline port (``app/workers/pipeline_port.py``, owned by B1).

The pipeline is written structurally against these names and signatures so it never imports the
worker package (the worker imports the pipeline by dotted path, not the other way round). At merge
time B1's module is the single source of truth: replace this file with
``from app.workers.pipeline_port import *`` (or re-export) and nothing else has to change.
"""

from __future__ import annotations

import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from sqlalchemy.orm import Session

PipelineOutcome = Literal["succeeded", "unsupported", "no_text"]


class LeaseLost(Exception):
    """The worker no longer owns the job (expired lease, newer epoch, cancelled). Stop quietly."""


class RetryableJobError(Exception):
    """A transient failure: the job goes back to ``queued`` with backoff. ``code`` is stable."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class PermanentJobError(Exception):
    """A failure retrying cannot fix: the job becomes ``dead`` with ``code``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """What ``ingest_claim_job`` returned, plus the owner string the worker claimed with."""

    job_id: uuid.UUID
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    revision_id: uuid.UUID
    attempts: int
    lease_epoch: int
    lease_owner: str


@dataclass(frozen=True, slots=True)
class PipelineResult:
    outcome: PipelineOutcome
    detail: str = ""


class WorkerSessions(Protocol):
    """Open one worker-role transaction with ``app.current_org_id`` set (commit on exit)."""

    def __call__(self, *, org_id: uuid.UUID) -> AbstractContextManager[Session]: ...


@runtime_checkable
class JobContext(Protocol):
    @property
    def settings(self) -> Any: ...

    @property
    def session_factory(self) -> WorkerSessions: ...

    def heartbeat(self) -> None:
        """Extend the lease. Call between units of work (every few seconds at most)."""
        ...

    def check_fence(self) -> None:
        """Raise :class:`LeaseLost` when the job's lease/epoch is no longer ours."""
        ...


@runtime_checkable
class PipelineRunner(Protocol):
    def run(self, claim: ClaimedJob, ctx: JobContext) -> PipelineResult: ...


@runtime_checkable
class DocumentDeletedHandler(Protocol):
    def handle(self, org_id: uuid.UUID, document_id: uuid.UUID) -> None: ...
