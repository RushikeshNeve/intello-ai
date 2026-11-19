"""The seam between the worker runtime (this package) and the ingestion pipeline (M03.02/.03).

The runtime claims a job, keeps its lease alive, fences every state write and finalizes it; the
pipeline only does the stages. ``PipelineRunner.run`` must:

* call ``ctx.check_fence()`` before each stage and before any externally visible write, and let
  :class:`LeaseLost` propagate (never catch it);
* condition every database write it performs on the claim's fencing token, i.e.
  ``lease_owner = claim.lease_owner AND lease_epoch = claim.lease_epoch AND state = 'running'`` on
  the job row (``app.workers.leases.job_is_ours`` checks it inside the caller's transaction and
  raises :class:`LeaseLost`);
* raise :class:`RetryableJobError` for transient failures, :class:`PermanentJobError` for
  unrecoverable ones, and return a :class:`PipelineResult` otherwise. Anything else it raises is
  treated as a retryable ``internal_error`` (only the exception class name is stored).

Real implementations are resolved from settings by dotted path ``module:factory``; the factory is
called as ``factory(settings)`` (see :func:`resolve_factory`).
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sqlalchemy.orm import Session

from app.core.config import Settings

Outcome = Literal["succeeded", "unsupported", "no_text"]


class JobError(Exception):
    """Base of the errors a pipeline may raise. ``code`` is a stable, content-free identifier."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class RetryableJobError(JobError):
    """Transient (storage/RushDB/embedding server down, timeout): re-queue with backoff."""


class PermanentJobError(JobError):
    """Unrecoverable (corrupt file, limits): the job is ``dead`` at once, no retries."""


class LeaseLost(Exception):
    """The job's lease or epoch is no longer ours: stop, write nothing further."""


@dataclass(frozen=True)
class ClaimedJob:
    """The row ``ingest_claim_job`` returned, plus the owner string the claim was made with."""

    job_id: uuid.UUID
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    revision_id: uuid.UUID
    attempts: int
    lease_epoch: int
    lease_owner: str


@dataclass(frozen=True)
class PipelineResult:
    """``succeeded``; ``unsupported`` (detail = error code, e.g. ``unsupported_type``); ``no_text``
    (ends as ``unsupported`` with code ``no_text_extracted``)."""

    outcome: Outcome
    detail: str | None = None


@dataclass(frozen=True)
class JobContext:
    """What the runtime hands the pipeline.

    ``session_factory(org_id)`` opens a worker-role transaction with ``app.current_org_id`` set
    (commits on success, rolls back on error). ``heartbeat()`` extends the lease now and
    ``check_fence()`` is a cheap local check; both raise :class:`LeaseLost` once the lease or epoch
    is gone (or could not be renewed for a whole lease period). ``set_stage`` records the current
    stage on the job (fenced; raises :class:`LeaseLost`).
    """

    session_factory: Callable[[uuid.UUID], AbstractContextManager[Session]]
    heartbeat: Callable[[], None]
    check_fence: Callable[[], None]
    set_stage: Callable[[str], None]
    settings: Settings


class PipelineRunner(Protocol):
    def run(self, claim: ClaimedJob, ctx: JobContext) -> PipelineResult: ...


class DocumentDeletedHandler(Protocol):
    def handle(self, org_id: uuid.UUID, document_id: uuid.UUID) -> None: ...


class ReconcileHandler(Protocol):
    """Drift repair of one document's publication (contract §9); also finishes the flag flip and
    the purge after ``knowledge.revision.published``. Must be idempotent."""

    def handle(self, org_id: uuid.UUID, document_id: uuid.UUID) -> None: ...


class WorkerConfigError(RuntimeError):
    """A configured implementation cannot be loaded: a clear startup error, not a task failure."""


def resolve_factory(path: str, *, setting: str) -> Callable[..., Any]:
    """Import ``module:attr`` (or ``module.attr``) and return the callable."""
    module_name, sep, attr = path.partition(":")
    if not sep:
        module_name, _, attr = path.rpartition(".")
    if not module_name or not attr:
        raise WorkerConfigError(f"{setting}={path!r} is not a dotted path 'module:factory'")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise WorkerConfigError(
            f"{setting}={path!r}: cannot import module {module_name!r} "
            f"({exc.__class__.__name__}: {exc}); the implementation is not part of this build"
        ) from exc
    target = getattr(module, attr, None)
    if not callable(target):
        raise WorkerConfigError(f"{setting}={path!r}: {module_name!r} has no callable {attr!r}")
    return target  # type: ignore[no-any-return]
