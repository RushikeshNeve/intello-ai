"""The ingestion job runtime: claim, heartbeat, run the pipeline, finalize (M03.01.03/.04).

Pure orchestration, independent of Celery: ``run_ingest_job`` is what the ``ingest.run_job`` task
calls, and what the tests call directly with threads to model zombie workers. It never logs
document content; failures are stored as stable codes plus a redacted, truncated detail.
"""

from __future__ import annotations

import logging
import random
import uuid
from collections.abc import Callable
from typing import Literal

from sqlalchemy import Engine

from app.core.config import Settings
from app.workers import leases
from app.workers.pipeline_port import (
    ClaimedJob,
    JobContext,
    LeaseLost,
    PermanentJobError,
    PipelineResult,
    PipelineRunner,
    RetryableJobError,
)
from app.workers.worker_db import session_factory_for

log = logging.getLogger(__name__)

RunOutcome = Literal[
    "skipped",  # nothing to claim: duplicate delivery, running elsewhere, finished or not due
    "succeeded",
    "unsupported",
    "retry_scheduled",
    "dead",
    "lease_lost",  # we were fenced out: nothing further was written
]

# Raised by Celery's soft time limit; imported lazily so this module has no Celery dependency.
_SOFT_TIMEOUT_NAMES = frozenset({"SoftTimeLimitExceeded"})


def _is_timeout(exc: BaseException) -> bool:
    return type(exc).__name__ in _SOFT_TIMEOUT_NAMES or isinstance(exc, TimeoutError)


def run_ingest_job(
    job_id: uuid.UUID,
    *,
    engine: Engine,
    settings: Settings,
    runner: PipelineRunner,
    owner: str | None = None,
    rng: Callable[[], float] | None = None,
) -> RunOutcome:
    """Claim ``job_id`` and drive it to a final or re-queued state (job failures never raise)."""
    owner = owner or leases.new_owner()
    claim = leases.claim_job(engine, job_id, owner, settings.INGEST_LEASE_SECONDS)
    if claim is None:
        log.info("ingest job=%s: nothing to claim (duplicate or not due); acking", job_id)
        return "skipped"
    log.info(
        "ingest job=%s attempt=%s epoch=%s claimed", claim.job_id, claim.attempts, claim.lease_epoch
    )

    heartbeater = leases.Heartbeater(
        engine,
        claim,
        lease_seconds=settings.INGEST_LEASE_SECONDS,
        interval_seconds=settings.INGEST_HEARTBEAT_SECONDS,
    ).start()

    def set_stage(stage: str) -> None:
        heartbeater.check()
        leases.set_stage(engine, claim, stage)

    ctx = JobContext(
        session_factory=session_factory_for(engine),
        heartbeat=heartbeater.beat,
        check_fence=heartbeater.check,
        set_stage=set_stage,
        settings=settings,
    )
    try:
        try:
            result = runner.run(claim, ctx)
            return _finalize_result(engine, claim, heartbeater, result)
        except LeaseLost:
            log.warning("ingest job=%s epoch=%s: lease lost, stopping", job_id, claim.lease_epoch)
            return "lease_lost"
        except PermanentJobError as exc:
            heartbeater.check()
            leases.finish_dead(engine, claim, exc.code, exc.detail, reason="permanent")
            log.warning("ingest job=%s dead (permanent) code=%s", job_id, exc.code)
            return "dead"
        except RetryableJobError as exc:
            return _retry(engine, claim, heartbeater, settings, exc.code, exc.detail, rng)
        except Exception as exc:  # noqa: BLE001 - any pipeline bug is a retryable failure
            # Only the class name is stored: exception messages may carry document content.
            code = "timeout" if _is_timeout(exc) else "internal_error"
            log.warning("ingest job=%s failed code=%s type=%s", job_id, code, type(exc).__name__)
            return _retry(engine, claim, heartbeater, settings, code, type(exc).__name__, rng)
    except LeaseLost:  # raised by a finalizer: fenced out while finishing
        log.warning(
            "ingest job=%s epoch=%s: lease lost while finalizing", job_id, claim.lease_epoch
        )
        return "lease_lost"
    finally:
        heartbeater.stop()


def _finalize_result(
    engine: Engine, claim: ClaimedJob, hb: leases.Heartbeater, result: PipelineResult
) -> RunOutcome:
    hb.check()
    if result.outcome == "succeeded":
        leases.finish_succeeded(engine, claim)
        return "succeeded"
    if result.outcome == "no_text":
        leases.finish_unsupported(engine, claim, "no_text_extracted", result.detail)
        return "unsupported"
    leases.finish_unsupported(engine, claim, result.detail or "unsupported_type", None)
    return "unsupported"


def _retry(
    engine: Engine,
    claim: ClaimedJob,
    hb: leases.Heartbeater,
    settings: Settings,
    code: str,
    detail: str | None,
    rng: Callable[[], float] | None,
) -> RunOutcome:
    hb.check()
    outcome = leases.schedule_retry(
        engine,
        claim,
        code,
        detail,
        base_seconds=settings.INGEST_RETRY_BASE_SECONDS,
        rng=rng or random.random,
    )
    return "retry_scheduled" if outcome == "retry" else "dead"
