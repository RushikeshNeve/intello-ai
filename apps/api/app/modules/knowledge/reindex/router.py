"""Re-index admin API (M12.05): org owners/admins only (``org.update``).

``POST /v1/orgs/{o}/reindex-jobs``                 plan a job into the configured embedding space
``GET  /v1/orgs/{o}/reindex-jobs/{id}``            state and counts
``POST /v1/orgs/{o}/reindex-jobs/{id}/resume``     (re)queue the worker (after a crash/failure)
``POST /v1/orgs/{o}/reindex-jobs/{id}/cutover``    atomic pointer flip (job must be ``ready``)
``POST /v1/orgs/{o}/reindex-jobs/{id}/rollback``   atomic flip back (after a cutover)
``POST /v1/orgs/{o}/reindex-jobs/{id}/purge-old``  delete the abandoned space (irreversible)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import CONFLICT, Tenant, api_error, not_found
from app.core.config import get_settings
from app.modules.ai.embeddings.registry import spec_from_settings
from app.modules.audit import AuditEventType, record_event
from app.modules.authorization.policy import Action
from app.modules.knowledge.reindex import service
from app.modules.organizations import service as org_service

router = APIRouter(prefix="/v1/orgs/{org_id}/reindex-jobs", tags=["reindex"])
TARGET_REINDEX = "reindex_job"


class ReindexJob(BaseModel):
    id: uuid.UUID
    from_space: str
    to_space: str
    state: str
    total: int
    done: int
    failed: int
    last_error_code: str | None = None
    old_space_purged: bool = False
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


def _out(row: dict[str, Any]) -> ReindexJob:
    return ReindexJob(
        **{k: row[k] for k in ReindexJob.model_fields if k in row},
        old_space_purged=row["old_space_purged_at"] is not None,
    )


def default_enqueue(org_id: uuid.UUID, job_id: uuid.UUID) -> None:
    from app.workers.celery_app import celery_app

    celery_app.send_task(
        "maintenance.reindex_run", args=[str(org_id), str(job_id)], queue="maintenance"
    )


def default_purge(org_id: uuid.UUID, job_id: uuid.UUID) -> int:
    from app.modules.knowledge.reindex.tasks import purge_default

    return purge_default(str(org_id), str(job_id))


# Replaceable in tests: how a job is handed to the worker and how the old space is purged.
HOOKS: dict[str, Any] = {"enqueue": default_enqueue, "purge": default_purge}


def _admin(tenant: Any, session: Any, org_id: uuid.UUID) -> None:
    access = org_service.load_org_access(session, org_id, tenant.user_id)
    if access is None:
        raise not_found()
    access.require(Action.ORG_UPDATE)


def _audit(tenant: Any, session: Any, org_id: uuid.UUID, event: str, job_id: uuid.UUID) -> None:
    record_event(
        session,
        event_type=event,
        actor_user_id=tenant.user_id,
        org_id=org_id,
        target_type=TARGET_REINDEX,
        target_id=str(job_id),
        outcome="success",
        request_id=tenant.request_id,
    )


def _job(session: Any, org_id: uuid.UUID, job_id: uuid.UUID) -> dict[str, Any]:
    row = service.get_job(session, org_id, job_id)
    if row is None:
        raise not_found()
    return row


@router.post("", response_model=ReindexJob, status_code=status.HTTP_201_CREATED)
def create_job(tenant: Tenant, org_id: uuid.UUID) -> ReindexJob:
    spec = spec_from_settings(get_settings())
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id)
        try:
            job_id = service.create_job(
                session,
                org_id,
                to_space=spec.space_id,
                to_dimensions=spec.dimensions,
                default_space=spec.space_id,
                requested_by=tenant.user_id,
            )
        except service.ReindexError as exc:
            raise api_error(status.HTTP_409_CONFLICT, exc.code) from None
        _audit(tenant, session, org_id, AuditEventType.REINDEX_CREATED, job_id)
        row = _job(session, org_id, job_id)
    HOOKS["enqueue"](org_id, job_id)
    return _out(row)


@router.get("/{job_id}", response_model=ReindexJob)
def get_job(tenant: Tenant, org_id: uuid.UUID, job_id: uuid.UUID) -> ReindexJob:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id)
        return _out(_job(session, org_id, job_id))


@router.post("/{job_id}/resume", response_model=ReindexJob)
def resume_job(tenant: Tenant, org_id: uuid.UUID, job_id: uuid.UUID) -> ReindexJob:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id)
        row = _job(session, org_id, job_id)
    if row["state"] not in ("planned", "running", "verifying", "failed"):
        raise api_error(status.HTTP_409_CONFLICT, CONFLICT)
    HOOKS["enqueue"](org_id, job_id)
    return _out(row)


def _flip(tenant: Any, org_id: uuid.UUID, job_id: uuid.UUID, fn: str, event: str) -> ReindexJob:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id)
        result = session.execute(text(f"SELECT {fn}(:j)"), {"j": job_id}).scalar_one()
        if result == "not_found":
            raise not_found()
        if result != "ok":
            raise api_error(status.HTTP_409_CONFLICT, str(result))
        _audit(tenant, session, org_id, event, job_id)
        return _out(_job(session, org_id, job_id))


@router.post("/{job_id}/cutover", response_model=ReindexJob)
def cutover(tenant: Tenant, org_id: uuid.UUID, job_id: uuid.UUID) -> ReindexJob:
    return _flip(tenant, org_id, job_id, "reindex_cutover", AuditEventType.REINDEX_CUTOVER)


@router.post("/{job_id}/rollback", response_model=ReindexJob)
def rollback(tenant: Tenant, org_id: uuid.UUID, job_id: uuid.UUID) -> ReindexJob:
    return _flip(tenant, org_id, job_id, "reindex_rollback", AuditEventType.REINDEX_ROLLBACK)


@router.post("/{job_id}/purge-old", response_model=ReindexJob)
def purge_old(tenant: Tenant, org_id: uuid.UUID, job_id: uuid.UUID) -> ReindexJob:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id)
        row = _job(session, org_id, job_id)
        if row["state"] not in ("cutover", "rolled_back") or row["old_space_purged_at"]:
            raise api_error(status.HTTP_409_CONFLICT, "bad_state")
        _audit(tenant, session, org_id, AuditEventType.REINDEX_PURGED, job_id)
    HOOKS["purge"](org_id, job_id)
    with tenant.transaction(org_id) as session:
        return _out(_job(session, org_id, job_id))
