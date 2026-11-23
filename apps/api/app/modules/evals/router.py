"""Workspace golden sets (M12.03, feature E1). Contract: docs/architecture/m12-contracts.md.

Everything is workspace-admin only (``workspace.update``); a non-member / other-org caller gets the
audited concealed 404, a viewer/editor a 403. A run is SYNCHRONOUS (at most 50 cases per set, so
bounded) and retrieval-only: it calls the real retrieval service with a scope narrowed to this one
workspace, so it can never read another workspace's documents.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy.orm import Session

from app.api.deps import Tenant, TenantRequest, api_error, not_found
from app.modules.authorization.policy import Action
from app.modules.evals import service
from app.modules.evals.schemas import (
    CaseIn,
    CaseList,
    CaseOut,
    CasePatch,
    ResultOut,
    RunCreate,
    RunList,
    RunOut,
    SetCreate,
    SetList,
    SetOut,
)
from app.modules.firewall import abuse
from app.modules.knowledge.ports import UnknownOrganization
from app.modules.organizations.service import load_org_access
from app.modules.retrieval import service as retrieval
from app.modules.retrieval.hybrid import SearchUnavailable
from app.modules.retrieval.scope import EmptyScope, resolve_scope
from app.modules.retrieval.wiring import EmbedderDep, RepoDep, search_unavailable
from app.modules.workspaces import service as ws_service

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}/evals", tags=["evals"])

TARGET_EVAL = "eval_set"


def _admin(tenant: TenantRequest, session: Session, org_id: uuid.UUID, ws: uuid.UUID) -> None:
    access = load_org_access(session, org_id, tenant.user_id)
    if access is None:
        raise not_found()
    if access.role is None:
        access.require(Action.WORKSPACE_UPDATE, target_type=TARGET_EVAL, target_id=str(ws))
    found = ws_service.load_workspace(session, org_id, ws, tenant.user_id)
    if found is None:
        raise not_found()
    access = replace(access, subject=replace(access.subject, workspace_role=found[1]))
    access.require(Action.WORKSPACE_UPDATE, target_type=TARGET_EVAL, target_id=str(ws))


def _set_or_404(session: Session, ws: uuid.UUID, set_id: uuid.UUID) -> dict[str, Any]:
    row = service.get_set(session, ws, set_id)
    if row is None:
        raise not_found()
    return row


# ------------------------------------------------------------------------------------- sets


@router.get("/sets", response_model=SetList)
def list_sets(tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID) -> SetList:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id, workspace_id)
        return SetList(items=[SetOut(**r) for r in service.list_sets(session, workspace_id)])


@router.post("/sets", response_model=SetOut, status_code=status.HTTP_201_CREATED)
def create_set(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, body: SetCreate
) -> SetOut:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id, workspace_id)
        set_id = service.create_set(session, org_id, workspace_id, tenant.user_id, body.name)
        return SetOut(**_set_or_404(session, workspace_id, set_id))


@router.get("/sets/{set_id}", response_model=SetOut)
def get_set(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, set_id: uuid.UUID
) -> SetOut:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id, workspace_id)
        return SetOut(**_set_or_404(session, workspace_id, set_id))


@router.patch("/sets/{set_id}", response_model=SetOut)
def rename_set(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, set_id: uuid.UUID, body: SetCreate
) -> SetOut:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id, workspace_id)
        _set_or_404(session, workspace_id, set_id)
        service.rename_set(session, workspace_id, set_id, body.name)
        return SetOut(**_set_or_404(session, workspace_id, set_id))


@router.delete("/sets/{set_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_set(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, set_id: uuid.UUID
) -> Response:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id, workspace_id)
        _set_or_404(session, workspace_id, set_id)
        service.delete_set(session, workspace_id, set_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------------------------ cases


@router.get("/sets/{set_id}/cases", response_model=CaseList)
def list_cases(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, set_id: uuid.UUID
) -> CaseList:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id, workspace_id)
        _set_or_404(session, workspace_id, set_id)
        return CaseList(
            items=[CaseOut(**r) for r in service.list_cases(session, workspace_id, set_id)]
        )


@router.post("/sets/{set_id}/cases", response_model=CaseOut, status_code=status.HTTP_201_CREATED)
def add_case(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, set_id: uuid.UUID, body: CaseIn
) -> CaseOut:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id, workspace_id)
        _set_or_404(session, workspace_id, set_id)
        try:
            case_id = service.add_case(session, org_id, workspace_id, set_id, body)
        except service.SetFull:
            raise api_error(status.HTTP_409_CONFLICT, "set_full") from None
        row = service.get_case(session, workspace_id, set_id, case_id)
        assert row is not None  # noqa: S101 - just inserted
        return CaseOut(**row)


@router.patch("/sets/{set_id}/cases/{case_id}", response_model=CaseOut)
def patch_case(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    set_id: uuid.UUID,
    case_id: uuid.UUID,
    body: CasePatch,
) -> CaseOut:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id, workspace_id)
        _set_or_404(session, workspace_id, set_id)
        if service.get_case(session, workspace_id, set_id, case_id) is None:
            raise not_found()
        service.patch_case(session, workspace_id, set_id, case_id, body)
        row = service.get_case(session, workspace_id, set_id, case_id)
        assert row is not None  # noqa: S101
        return CaseOut(**row)


@router.delete("/sets/{set_id}/cases/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_case(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    set_id: uuid.UUID,
    case_id: uuid.UUID,
) -> Response:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id, workspace_id)
        _set_or_404(session, workspace_id, set_id)
        if service.get_case(session, workspace_id, set_id, case_id) is None:
            raise not_found()
        service.delete_case(session, workspace_id, set_id, case_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------------------------- runs


@router.post("/sets/{set_id}/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
def start_run(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    set_id: uuid.UUID,
    body: RunCreate,
    repo: RepoDep,
    embedder: EmbedderDep,
) -> RunOut:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id, workspace_id)
        eval_set = _set_or_404(session, workspace_id, set_id)
        cases = service.list_cases(session, workspace_id, set_id)
        if not cases:
            raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "empty_set")
        try:  # scope = THIS workspace only, whatever else the caller may read
            scope = resolve_scope(
                session,
                org_id=org_id,
                user_id=tenant.user_id,
                requested_workspace_ids=[workspace_id],
            )
        except (EmptyScope, UnknownOrganization):
            raise not_found() from None
        # M14.07: canary documents never count toward golden-set metrics.
        canaries = abuse.canary_document_ids(session, org_id)
        results: list[dict[str, Any]] = []
        for case in cases:
            retrieved: list[dict[str, Any]] = []
            status_: str = "answered"
            try:
                outcome = retrieval.retrieve(
                    session,
                    scope,
                    repo,
                    embedder,
                    case["question"],
                    top_k=body.top_k,
                    mode=body.mode,
                )
                retrieved = [
                    {
                        "document_id": str(e.document_id),
                        "chunk_id": str(e.chunk_id),
                        "title": e.title,
                        "score": round(e.score, 6),
                    }
                    for e in outcome.evidence
                    if e.workspace_id == workspace_id
                    and e.score >= body.min_score
                    and e.document_id not in canaries
                ]
                if not retrieved:
                    status_ = "abstained"
            except SearchUnavailable:
                raise search_unavailable() from None
            except ValueError:  # the question is not searchable
                status_ = "error"
            metrics = service.case_metrics(case, retrieved, answered=status_ == "answered")
            results.append(
                {
                    "case_id": case["id"],
                    "question": case["question"],
                    "retrieved": retrieved,
                    "answer_status": status_,
                    "metrics": metrics,
                }
            )
        config: dict[str, Any] = {
            "top_k": body.top_k,
            "min_score": body.min_score,
            "mode": body.mode,
            "retrieval": "workspace_scoped",
            # Reserved for LLM-in-the-loop runs; a retrieval-only run records no model.
            "model": None,
            "policy": None,
            "prompt_version": None,
        }
        run_id = service.insert_run(
            session,
            org=org_id,
            ws=workspace_id,
            user=tenant.user_id,
            set_id=set_id,
            set_version=eval_set["version"],
            config=config,
            summary=service.summarize(results, top_k=body.top_k),
            results=results,
        )
        run = service.get_run(session, workspace_id, set_id, run_id)
    assert run is not None  # noqa: S101
    return _run_out(run)


def _run_out(run: dict[str, Any]) -> RunOut:
    results = run.pop("results", None)
    return RunOut(
        **run,
        results=None if results is None else [ResultOut(**r) for r in results],
    )


@router.get("/sets/{set_id}/runs", response_model=RunList)
def list_runs(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, set_id: uuid.UUID
) -> RunList:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id, workspace_id)
        _set_or_404(session, workspace_id, set_id)
        return RunList(
            items=[_run_out(r) for r in service.list_runs(session, workspace_id, set_id)]
        )


@router.get("/sets/{set_id}/runs/{run_id}", response_model=RunOut)
def get_run(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    set_id: uuid.UUID,
    run_id: uuid.UUID,
) -> RunOut:
    with tenant.transaction(org_id) as session:
        _admin(tenant, session, org_id, workspace_id)
        _set_or_404(session, workspace_id, set_id)
        run = service.get_run(session, workspace_id, set_id, run_id)
        if run is None:
            raise not_found()
    return _run_out(run)
