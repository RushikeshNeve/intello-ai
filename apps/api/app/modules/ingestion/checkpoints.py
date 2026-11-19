"""Stage checkpoints and the fencing primitive (M03.02.04, contract §6).

* ``stage_fingerprint`` = sha256 over the stage name, the sha256 of the stage's input (the previous
  stage's artifact, or the source file for ``extract``) and the stage parameters (pipeline version,
  embedding space, chunk sizes...). Any change of input or parameters changes the fingerprint.
* A stage is **skipped on resume** only when its checkpoint is ``completed``, the fingerprint is
  unchanged and (artifact stages) the artifact object still exists with the recorded sha256
  (verified on read). Anything else recomputes and overwrites the checkpoint.
* Every database write is **fenced**: :func:`fence` updates the job row conditioned on
  ``lease_owner``, ``lease_epoch`` and ``state = 'running'`` in the same transaction as the write
  and raises ``LeaseLost`` on zero rows. A worker that lost its lease can therefore not record a
  checkpoint, a revision state or a publication.

Short transactions only: compute happens outside any transaction, so no connection is held while
extracting, embedding or talking to the index.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.modules.ingestion.artifacts import (
    ArtifactStore,
    artifact_content_type,
    artifact_key,
    sha256_hex,
)
from app.modules.ingestion.contracts import canonical_json_bytes
from app.modules.ingestion.models import (
    ERROR_DETAIL_MAX_LENGTH,
    IngestionCheckpoint,
    IngestionJobEvent,
)
from app.modules.ingestion.runner_types import ClaimedJob, JobContext, LeaseLost
from app.workers.leases import job_is_ours

CHECKPOINT_RUNNING = "running"
CHECKPOINT_COMPLETED = "completed"
CHECKPOINT_FAILED = "failed"


def stage_fingerprint(stage: str, input_sha256: str, parameters: Mapping[str, Any]) -> str:
    """The input fingerprint of a stage (contract §6). Parameters must be JSON values."""
    payload = {"stage": stage, "input": input_sha256, "parameters": dict(parameters)}
    return sha256_hex(canonical_json_bytes(payload))


# ------------------------------------------------------------------------------------ fencing


def fence(session: Session, claim: ClaimedJob, *, stage: str | None = None) -> None:
    """Prove in this transaction that the claim is still current; lock the job row until commit.

    Raises :class:`LeaseLost` when the job is no longer ``running`` under this owner and epoch
    (expired and reaped, re-claimed by another worker, cancelled by a deletion...).
    """
    job_is_ours(session, claim)  # FOR UPDATE: the job row stays locked until commit
    if stage is not None:
        session.execute(
            text(
                "UPDATE ingestion_jobs SET updated_at = now(), current_stage = :stage"
                " WHERE id = :job"
            ),
            {"stage": stage, "job": claim.job_id},
        )


def add_job_event(
    session: Session,
    claim: ClaimedJob,
    event_type: str,
    *,
    stage: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> None:
    session.execute(
        insert(IngestionJobEvent).values(
            job_id=claim.job_id,
            org_id=claim.org_id,
            workspace_id=claim.workspace_id,
            document_id=claim.document_id,
            version_id=claim.version_id,
            event_type=event_type,
            stage=stage,
            attempt=claim.attempts,
            detail=dict(detail or {}),
        )
    )


# ------------------------------------------------------------------------------- checkpoint rows


@dataclass(frozen=True, slots=True)
class Checkpoint:
    stage: str
    state: str
    input_fingerprint: str
    artifact_key: str | None
    artifact_sha256: str | None
    artifact_bytes: int | None
    attempts: int


def get_checkpoint(session: Session, revision_id: uuid.UUID, stage: str) -> Checkpoint | None:
    row = session.execute(
        select(
            IngestionCheckpoint.stage,
            IngestionCheckpoint.state,
            IngestionCheckpoint.input_fingerprint,
            IngestionCheckpoint.artifact_key,
            IngestionCheckpoint.artifact_sha256,
            IngestionCheckpoint.artifact_bytes,
            IngestionCheckpoint.attempts,
        ).where(IngestionCheckpoint.revision_id == revision_id, IngestionCheckpoint.stage == stage)
    ).first()
    if row is None:
        return None
    return Checkpoint(*row)


def reusable(cp: Checkpoint | None, fingerprint: str) -> bool:
    """Completed *and* computed from exactly this input/parameters."""
    return (
        cp is not None and cp.state == CHECKPOINT_COMPLETED and cp.input_fingerprint == fingerprint
    )


def begin_checkpoint(session: Session, claim: ClaimedJob, stage: str, fingerprint: str) -> None:
    """Upsert the checkpoint as ``running`` for ``fingerprint`` (clears any previous artifact)."""
    table = IngestionCheckpoint.__table__
    stmt = pg_insert(IngestionCheckpoint).values(
        revision_id=claim.revision_id,
        stage=stage,
        org_id=claim.org_id,
        state=CHECKPOINT_RUNNING,
        input_fingerprint=fingerprint,
        attempts=1,
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[table.c.revision_id, table.c.stage],
            set_={
                "state": CHECKPOINT_RUNNING,
                "input_fingerprint": fingerprint,
                "artifact_key": None,
                "artifact_sha256": None,
                "artifact_bytes": None,
                "attempts": table.c.attempts + 1,
                "started_at": text("now()"),
                "completed_at": None,
                "error_code": None,
            },
        )
    )


def complete_checkpoint(
    session: Session,
    claim: ClaimedJob,
    stage: str,
    fingerprint: str,
    *,
    key: str | None = None,
    sha256: str | None = None,
    size: int | None = None,
) -> None:
    updated = session.execute(
        update(IngestionCheckpoint)
        .where(
            IngestionCheckpoint.revision_id == claim.revision_id,
            IngestionCheckpoint.stage == stage,
            IngestionCheckpoint.input_fingerprint == fingerprint,
        )
        .values(
            state=CHECKPOINT_COMPLETED,
            artifact_key=key,
            artifact_sha256=sha256,
            artifact_bytes=size,
            completed_at=text("now()"),
            error_code=None,
        )
        .returning(IngestionCheckpoint.stage)
    ).first()
    if updated is None:  # the row was replaced by a newer fingerprint: this attempt is stale
        raise LeaseLost("checkpoint superseded")


def fail_checkpoint(
    session: Session, claim: ClaimedJob, stage: str, fingerprint: str, code: str
) -> None:
    session.execute(
        update(IngestionCheckpoint)
        .where(
            IngestionCheckpoint.revision_id == claim.revision_id,
            IngestionCheckpoint.stage == stage,
            IngestionCheckpoint.input_fingerprint == fingerprint,
        )
        .values(state=CHECKPOINT_FAILED, completed_at=None, error_code=code[:64])
    )


def invalidate_checkpoint(session: Session, claim: ClaimedJob, stage: str, code: str) -> None:
    """Make a stage non-reusable (its effect turned out to be missing); the caller fences."""
    session.execute(
        update(IngestionCheckpoint)
        .where(
            IngestionCheckpoint.revision_id == claim.revision_id,
            IngestionCheckpoint.stage == stage,
        )
        .values(state=CHECKPOINT_FAILED, completed_at=None, error_code=code[:64])
    )


# -------------------------------------------------------------------------------- stage runner


@dataclass(frozen=True, slots=True)
class StageOutput:
    """Result of one stage: the artifact bytes (artifact stages), its digest and whether the stage
    was skipped because a verified checkpoint already existed."""

    fingerprint: str
    sha256: str
    data: bytes | None
    skipped: bool


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    name = code if isinstance(code, str) and code else type(exc).__name__
    return name[:64]


class StageRunner:
    """Runs checkpointed stages of one claimed job (artifact stages and effect-only stages)."""

    def __init__(self, claim: ClaimedJob, ctx: JobContext, artifacts: ArtifactStore) -> None:
        self._claim = claim
        self._ctx = ctx
        self._artifacts = artifacts

    def _tx(self) -> Any:
        return self._ctx.session_factory(self._claim.org_id)

    def artifact_stage(
        self,
        stage: str,
        input_sha256: str,
        parameters: Mapping[str, Any],
        compute: Callable[[], bytes],
    ) -> StageOutput:
        """Run ``compute`` unless a completed, verified checkpoint for the same input exists."""
        claim = self._claim
        fingerprint = stage_fingerprint(stage, input_sha256, parameters)
        key = artifact_key(claim.org_id, claim.revision_id, stage)
        with self._tx() as session:
            fence(session, claim, stage=stage)
            cp = get_checkpoint(session, claim.revision_id, stage)
        if reusable(cp, fingerprint) and cp is not None and cp.artifact_key == key:
            data = self._artifacts.get_verified(
                key, sha256=cp.artifact_sha256, size=cp.artifact_bytes
            )
            if data is not None and cp.artifact_sha256 is not None:
                return StageOutput(fingerprint, cp.artifact_sha256, data, skipped=True)

        with self._tx() as session:
            fence(session, claim, stage=stage)
            begin_checkpoint(session, claim, stage, fingerprint)
            add_job_event(session, claim, "stage_started", stage=stage)
        try:
            data = compute()
            self._ctx.check_fence()
            stored = self._artifacts.put(key, data, content_type=artifact_content_type(stage))
        except Exception as exc:
            self._record_failure(stage, fingerprint, exc)
            raise
        with self._tx() as session:
            fence(session, claim, stage=stage)
            complete_checkpoint(
                session, claim, stage, fingerprint, key=key, sha256=stored.sha256, size=stored.size
            )
            add_job_event(
                session, claim, "stage_completed", stage=stage, detail={"bytes": stored.size}
            )
        return StageOutput(fingerprint, stored.sha256, data, skipped=False)

    def effect_stage(
        self,
        stage: str,
        input_sha256: str,
        parameters: Mapping[str, Any],
        compute: Callable[[], None],
        *,
        verify: Callable[[], bool] | None = None,
    ) -> StageOutput:
        """A stage whose product is an effect elsewhere (index, validate, publish).

        Skipped when completed with the same fingerprint **and** ``verify()`` (when given) still
        proves the effect exists; ``verify=None`` means "always run" (cheap, idempotent stages).
        """
        claim = self._claim
        fingerprint = stage_fingerprint(stage, input_sha256, parameters)
        with self._tx() as session:
            fence(session, claim, stage=stage)
            cp = get_checkpoint(session, claim.revision_id, stage)
        if verify is not None and reusable(cp, fingerprint) and verify():
            return StageOutput(fingerprint, fingerprint, None, skipped=True)

        with self._tx() as session:
            fence(session, claim, stage=stage)
            begin_checkpoint(session, claim, stage, fingerprint)
            add_job_event(session, claim, "stage_started", stage=stage)
        try:
            compute()
        except Exception as exc:
            self._record_failure(stage, fingerprint, exc)
            raise
        with self._tx() as session:
            fence(session, claim, stage=stage)
            complete_checkpoint(session, claim, stage, fingerprint)
            add_job_event(session, claim, "stage_completed", stage=stage)
        return StageOutput(fingerprint, fingerprint, None, skipped=False)

    def _record_failure(self, stage: str, fingerprint: str, exc: BaseException) -> None:
        """Best effort: a failure to record the failure must not hide the original error."""
        if isinstance(exc, LeaseLost):
            return
        try:
            with self._tx() as session:
                fence(session, self._claim, stage=stage)
                fail_checkpoint(session, self._claim, stage, fingerprint, _error_code(exc))
        except Exception:  # noqa: BLE001, S110 - the original exception is the one that matters
            pass


__all__ = [
    "CHECKPOINT_COMPLETED",
    "CHECKPOINT_FAILED",
    "CHECKPOINT_RUNNING",
    "ERROR_DETAIL_MAX_LENGTH",
    "Checkpoint",
    "StageOutput",
    "StageRunner",
    "add_job_event",
    "begin_checkpoint",
    "complete_checkpoint",
    "fail_checkpoint",
    "fence",
    "get_checkpoint",
    "invalidate_checkpoint",
    "reusable",
    "stage_fingerprint",
]
