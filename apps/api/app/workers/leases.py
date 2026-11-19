"""Job claims, leases, heartbeats and fenced state transitions (M03.01.04; contract §4, §6).

Fencing token = ``(lease_owner, lease_epoch)`` returned by the claim. EVERY write a worker makes to
a job after the claim is conditioned on ``lease_owner = me AND lease_epoch = <mine> AND state =
'running'``; a worker that lost its lease (expired and reaped, cancelled, re-claimed by another
worker) matches no row and gets :class:`LeaseLost`: it must stop and write nothing more. Leaving
``running`` clears the lease in the same statement (``ck_ingestion_jobs_lease``).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import socket
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, Final, Literal

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.modules.ingestion.models import STAGES
from app.workers.pipeline_port import ClaimedJob, LeaseLost
from app.workers.worker_db import system_connection, worker_session

log = logging.getLogger(__name__)

RETRY_BACKOFF_CAP_SECONDS: Final = 3600
RETRY_JITTER: Final = 0.2
ERROR_DETAIL_MAX: Final = 500
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Fraction of the lease after which a worker that could not renew it treats it as gone. Below 1 so
# the local verdict is always more conservative than the database's.
LOCAL_LEASE_SAFETY: Final = 0.9

FENCE_SQL: Final = (
    "j.id = :job_id AND j.org_id = :org_id AND j.lease_owner = :owner"
    " AND j.lease_epoch = :epoch AND j.state = 'running'"
)


def backoff_seconds(
    attempts: int,
    base: int,
    *,
    cap: int = RETRY_BACKOFF_CAP_SECONDS,
    jitter: float = RETRY_JITTER,
    rng: Callable[[], float] = random.random,
) -> int:
    """``base * 2^(attempts-1)`` capped at ``cap``, with +/- ``jitter`` (contract §6)."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    raw = min(float(cap), float(base) * (2 ** min(attempts - 1, 30)))
    factor = 1.0 + jitter * (2.0 * rng() - 1.0)
    return int(max(0, min(cap, round(raw * factor))))


def new_owner() -> str:
    """A lease owner unique to one task execution (host:pid:random)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def clean_code(code: str) -> str:
    return code if _CODE_RE.fullmatch(code) else "invalid_error_code"


def clean_detail(detail: str | None) -> str | None:
    """Redacted, truncated error detail: control characters removed, at most 500 characters."""
    if detail is None:
        return None
    return _CONTROL_RE.sub(" ", detail)[:ERROR_DETAIL_MAX]


def _fence_params(claim: ClaimedJob) -> dict[str, Any]:
    return {
        "job_id": claim.job_id,
        "org_id": claim.org_id,
        "owner": claim.lease_owner,
        "epoch": claim.lease_epoch,
    }


def claim_job(
    engine: Engine, job_id: uuid.UUID, owner: str, lease_seconds: int
) -> ClaimedJob | None:
    """Claim ``job_id`` through ``ingest_claim_job``. ``None`` = nothing to do (duplicate delivery,
    already running or finished, not yet due): the caller acks and exits quietly."""
    with system_connection(engine) as conn:
        row = (
            conn.execute(
                text("SELECT * FROM ingest_claim_job(:j, :o, :l)"),
                {"j": job_id, "o": owner, "l": lease_seconds},
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    return ClaimedJob(
        job_id=row["job_id"],
        org_id=row["org_id"],
        workspace_id=row["workspace_id"],
        document_id=row["document_id"],
        version_id=row["version_id"],
        revision_id=row["revision_id"],
        attempts=row["attempts"],
        lease_epoch=row["lease_epoch"],
        lease_owner=owner,
    )


def job_is_ours(session: Session, claim: ClaimedJob, *, lock: bool = True) -> None:
    """Inside the caller's org-scoped transaction: raise :class:`LeaseLost` unless the job row still
    carries our fencing token; with ``lock`` the row stays locked until the transaction ends (the
    publication step needs that)."""
    suffix = " FOR UPDATE" if lock else ""
    found = session.execute(
        text(f"SELECT 1 FROM ingestion_jobs j WHERE {FENCE_SQL}{suffix}"),
        _fence_params(claim),
    ).first()
    if found is None:
        raise LeaseLost(f"job {claim.job_id} epoch {claim.lease_epoch} no longer ours")


def _event(
    session: Session, claim: ClaimedJob, event_type: str, detail: dict[str, Any] | None = None
) -> None:
    session.execute(
        text(
            "INSERT INTO ingestion_job_events (job_id, org_id, workspace_id, document_id,"
            " version_id, event_type, attempt, detail)"
            " VALUES (:job_id, :org_id, :ws, :doc, :ver, :type, :attempt,"
            " CAST(:detail AS jsonb))"
        ),
        {
            "job_id": claim.job_id,
            "org_id": claim.org_id,
            "ws": claim.workspace_id,
            "doc": claim.document_id,
            "ver": claim.version_id,
            "type": event_type,
            "attempt": claim.attempts,
            "detail": json.dumps(detail or {}),
        },
    )


def heartbeat_once(engine: Engine, claim: ClaimedJob, lease_seconds: int) -> None:
    """One fenced lease extension; :class:`LeaseLost` when the fence no longer matches."""
    with worker_session(engine, claim.org_id) as session:
        row = session.execute(
            text(
                "UPDATE ingestion_jobs j SET lease_expires_at = now() + make_interval(secs => :l),"
                " heartbeat_at = now(), updated_at = now()"
                f" WHERE {FENCE_SQL} RETURNING j.id"
            ),
            {**_fence_params(claim), "l": lease_seconds},
        ).first()
    if row is None:
        raise LeaseLost(f"job {claim.job_id}: heartbeat rejected (lease or epoch gone)")


def set_stage(engine: Engine, claim: ClaimedJob, stage: str) -> None:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    with worker_session(engine, claim.org_id) as session:
        row = session.execute(
            text(
                "UPDATE ingestion_jobs j SET current_stage = :stage, updated_at = now()"
                f" WHERE {FENCE_SQL} RETURNING j.id"
            ),
            {**_fence_params(claim), "stage": stage},
        ).first()
    if row is None:
        raise LeaseLost(f"job {claim.job_id}: stage write rejected (lease or epoch gone)")


class Heartbeater:
    """Background lease renewal plus the cheap local fence.

    ``check()`` raises :class:`LeaseLost` when a renewal was rejected, or when none succeeded for
    ``LOCAL_LEASE_SAFETY`` of a lease period (the database lease may already have lapsed and been
    handed to another worker). A transient database error does not by itself lose the lease: the
    thread keeps retrying until that local deadline passes.
    """

    def __init__(
        self,
        engine: Engine,
        claim: ClaimedJob,
        *,
        lease_seconds: int,
        interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._claim = claim
        self._lease = lease_seconds
        self._interval = interval_seconds
        self._clock = clock
        self._last_ok = clock()
        self._lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def start(self) -> Heartbeater:
        thread = threading.Thread(
            target=self._loop, name=f"heartbeat-{self._claim.job_id}", daemon=True
        )
        self._thread = thread
        thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def check(self) -> None:
        if self._lost.is_set():
            raise LeaseLost(f"job {self._claim.job_id}: lease lost")
        if self._clock() - self._last_ok >= self._lease * LOCAL_LEASE_SAFETY:
            self._lost.set()
            raise LeaseLost(f"job {self._claim.job_id}: lease could not be renewed in time")

    def beat(self) -> None:
        """Renew now (synchronously). Raises :class:`LeaseLost` if fenced out."""
        self.check()
        try:
            heartbeat_once(self._engine, self._claim, self._lease)
        except LeaseLost:
            self._lost.set()
            raise
        self._last_ok = self._clock()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.beat()
            except LeaseLost:
                log.warning(
                    "lease lost job=%s epoch=%s", self._claim.job_id, self._claim.lease_epoch
                )
                return
            except Exception as exc:  # noqa: BLE001 - keep trying until the local deadline
                log.warning(
                    "heartbeat failed job=%s error=%s", self._claim.job_id, type(exc).__name__
                )


# -- fenced finalization ----------------------------------------------------------------------

_LEAVE_RUNNING = (
    "lease_owner = NULL, lease_expires_at = NULL, current_stage = NULL, updated_at = now()"
)

FinalState = Literal["succeeded", "unsupported", "dead", "retry"]


def _finish(
    engine: Engine,
    claim: ClaimedJob,
    *,
    state: str,
    error_code: str | None,
    error_detail: str | None,
    event_type: str,
    event_detail: dict[str, Any],
) -> None:
    with worker_session(engine, claim.org_id) as session:
        row = session.execute(
            text(
                f"UPDATE ingestion_jobs j SET state = :state, {_LEAVE_RUNNING},"
                " finished_at = now(), last_error_code = :code, last_error_detail = :detail"
                f" WHERE {FENCE_SQL} RETURNING j.id"
            ),
            {**_fence_params(claim), "state": state, "code": error_code, "detail": error_detail},
        ).first()
        if row is None:
            raise LeaseLost(f"job {claim.job_id}: finalize rejected (lease or epoch gone)")
        _event(session, claim, event_type, event_detail)


def finish_succeeded(engine: Engine, claim: ClaimedJob) -> None:
    _finish(
        engine,
        claim,
        state="succeeded",
        error_code=None,
        error_detail=None,
        event_type="succeeded",
        event_detail={},
    )


def finish_unsupported(engine: Engine, claim: ClaimedJob, code: str, detail: str | None) -> None:
    safe = clean_code(code)
    _finish(
        engine,
        claim,
        state="unsupported",
        error_code=safe,
        error_detail=clean_detail(detail),
        event_type="unsupported",
        event_detail={"code": safe},
    )


def finish_dead(
    engine: Engine, claim: ClaimedJob, code: str, detail: str | None, *, reason: str
) -> None:
    safe = clean_code(code)
    _finish(
        engine,
        claim,
        state="dead",
        error_code=safe,
        error_detail=clean_detail(detail),
        event_type="dead",
        event_detail={"code": safe, "reason": reason},
    )


def schedule_retry(
    engine: Engine,
    claim: ClaimedJob,
    code: str,
    detail: str | None,
    *,
    base_seconds: int,
    rng: Callable[[], float] = random.random,
) -> Literal["retry", "dead"]:
    """Transient failure: back to ``queued`` with ``run_after`` backoff plus an outbox row (NOT a
    Celery retry; the database stays the truth), or ``dead`` once ``attempts >= max_attempts``."""
    safe = clean_code(code)
    clean = clean_detail(detail)
    with worker_session(engine, claim.org_id) as session:
        max_attempts = session.execute(
            text(f"SELECT j.max_attempts FROM ingestion_jobs j WHERE {FENCE_SQL} FOR UPDATE"),
            _fence_params(claim),
        ).scalar_one_or_none()
        if max_attempts is None:
            raise LeaseLost(f"job {claim.job_id}: retry rejected (lease or epoch gone)")
        if claim.attempts >= max_attempts:
            exhausted = True
        else:
            exhausted = False
            delay = backoff_seconds(claim.attempts, base_seconds, rng=rng)
            row = session.execute(
                text(
                    "UPDATE ingestion_jobs j SET state = 'queued',"
                    f" {_LEAVE_RUNNING},"
                    " run_after = now() + make_interval(secs => :delay),"
                    " last_error_code = :code, last_error_detail = :detail"
                    f" WHERE {FENCE_SQL} RETURNING j.run_after"
                ),
                {**_fence_params(claim), "delay": delay, "code": safe, "detail": clean},
            ).one()
            run_after = row[0]
            _event(
                session,
                claim,
                "retry_scheduled",
                {"code": safe, "backoff_seconds": delay},
            )
            session.execute(
                text(
                    "INSERT INTO outbox_events (org_id, aggregate_type, aggregate_id, event_type,"
                    " payload, available_at)"
                    " VALUES (:org_id, 'ingestion_job', :job_id, 'ingestion.job.queued',"
                    " CAST(:payload AS jsonb), :run_after)"
                ),
                {
                    "org_id": claim.org_id,
                    "job_id": claim.job_id,
                    "payload": json.dumps({"job_id": str(claim.job_id)}),
                    "run_after": run_after,
                },
            )
    if exhausted:
        finish_dead(engine, claim, safe, clean, reason="attempts_exhausted")
        return "dead"
    return "retry"


def reap_expired_leases(engine: Engine, backoff_base: int) -> int:
    with system_connection(engine) as conn:
        return int(
            conn.execute(
                text("SELECT ingest_reap_expired_leases(:b)"), {"b": backoff_base}
            ).scalar_one()
        )
