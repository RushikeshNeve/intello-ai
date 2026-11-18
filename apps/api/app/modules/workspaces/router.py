"""Workspace REST endpoints (M01.02.04, contract m01-contracts.md §3-§5).

Path resolution order: the org (missing/deleted -> 404) -> org membership (non-member -> audited
404) -> the workspace inside that org (missing/deleted/other org -> 404) -> ``decide()`` with the
caller's workspace role (no workspace role -> audited 404, insufficient role -> audited 403).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

from fastapi import APIRouter, Response, status
from sqlalchemy.orm import Session

from app.api.deps import NOT_ORG_MEMBER, Page, Tenant, TenantRequest, api_error, not_found
from app.modules.audit import AuditEventType, record_event
from app.modules.authorization.policy import Action, WorkspaceRole
from app.modules.organizations.service import OrgAccess, load_org_access
from app.modules.workspaces import service
from app.modules.workspaces.schemas import (
    Workspace,
    WorkspaceCreate,
    WorkspaceMember,
    WorkspaceMemberRoleIn,
    WorkspacePage,
    WorkspaceUpdate,
)
from app.modules.workspaces.service import TARGET_USER, TARGET_WORKSPACE, WorkspaceRow

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces", tags=["workspaces"])


@dataclass(frozen=True)
class WorkspaceAccess:
    org: OrgAccess  # its subject carries the caller's workspace role
    workspace: WorkspaceRow

    def require(
        self, action: Action, *, target_type: str = TARGET_WORKSPACE, target_id: str = ""
    ) -> None:
        self.org.require(
            action, target_type=target_type, target_id=target_id or str(self.workspace.id)
        )

    def view(self, row: WorkspaceRow | None = None) -> Workspace:
        row = row or self.workspace
        subject = self.org.subject
        return Workspace(
            id=row.id,
            org_id=row.org_id,
            slug=row.slug,
            name=row.name,
            created_at=row.created_at,
            role=service.effective_role(subject.org_role, subject.workspace_role),
        )


def _org_access(
    tenant: TenantRequest, session: Session, org_id: uuid.UUID, action: Action, target_id: str
) -> OrgAccess:
    access = load_org_access(session, org_id, tenant.user_id)
    if access is None:
        raise not_found()
    if access.role is None:
        # Not an org member: audited and concealed before anything about workspaces is revealed.
        access.require(action, target_type=TARGET_WORKSPACE, target_id=target_id)
    return access


def _workspace_access(
    tenant: TenantRequest,
    session: Session,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    action: Action,
) -> WorkspaceAccess:
    org = _org_access(tenant, session, org_id, action, str(workspace_id))
    found = service.load_workspace(session, org_id, workspace_id, tenant.user_id)
    if found is None:
        raise not_found()
    workspace, role = found
    access = WorkspaceAccess(
        org=replace(org, subject=replace(org.subject, workspace_role=role)), workspace=workspace
    )
    access.require(action)
    return access


def _audit(
    tenant: TenantRequest,
    session: Session,
    event_type: AuditEventType,
    *,
    org_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    metadata: dict[str, object],
) -> None:
    record_event(
        session,
        event_type=event_type,
        actor_user_id=tenant.user_id,
        org_id=org_id,
        target_type=target_type,
        target_id=str(target_id),
        outcome="success",
        request_id=tenant.request_id,
        metadata=metadata,
    )


@router.get("", response_model=WorkspacePage)
def list_workspaces(tenant: Tenant, org_id: uuid.UUID, page: Page) -> WorkspacePage:
    """``org.read``, filtered to the workspaces the caller may read."""
    with tenant.transaction(org_id) as session:
        access = load_org_access(session, org_id, tenant.user_id)
        if access is None:
            raise not_found()
        access.require(Action.ORG_READ)
        assert access.role is not None  # noqa: S101 - org.read allows members only
        items, next_cursor = service.list_readable(
            session, org_id, tenant.user_id, access.role, page
        )
    return WorkspacePage(
        items=[
            Workspace(
                id=w.id,
                org_id=w.org_id,
                slug=w.slug,
                name=w.name,
                created_at=w.created_at,
                role=role,
            )
            for w, role in items
        ],
        next_cursor=next_cursor,
    )


@router.post("", response_model=Workspace, status_code=status.HTTP_201_CREATED)
def create_workspace(tenant: Tenant, org_id: uuid.UUID, body: WorkspaceCreate) -> Workspace:
    """``workspace.create`` (org owner/admin); the creator becomes workspace admin."""
    with tenant.transaction(org_id) as session:
        access = load_org_access(session, org_id, tenant.user_id)
        if access is None:
            raise not_found()
        access.require(Action.WORKSPACE_CREATE)
        row = service.create_workspace(
            session, org_id=org_id, slug=body.slug, name=body.name, user_id=tenant.user_id
        )
        _audit(
            tenant,
            session,
            AuditEventType.WORKSPACE_CREATED,
            org_id=org_id,
            target_type=TARGET_WORKSPACE,
            target_id=row.id,
            metadata={"slug": row.slug, "name": row.name},
        )
    return Workspace(
        id=row.id,
        org_id=row.org_id,
        slug=row.slug,
        name=row.name,
        created_at=row.created_at,
        role=WorkspaceRole.ADMIN,
    )


@router.get("/{workspace_id}", response_model=Workspace)
def get_workspace(tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID) -> Workspace:
    with tenant.transaction(org_id) as session:
        access = _workspace_access(tenant, session, org_id, workspace_id, Action.WORKSPACE_READ)
    return access.view()


@router.patch("/{workspace_id}", response_model=Workspace)
def update_workspace(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, body: WorkspaceUpdate
) -> Workspace:
    with tenant.transaction(org_id) as session:
        access = _workspace_access(tenant, session, org_id, workspace_id, Action.WORKSPACE_UPDATE)
        row = access.workspace
        if body.name is not None and body.name != row.name:
            service.rename_workspace(session, org_id, workspace_id, body.name)
            _audit(
                tenant,
                session,
                AuditEventType.WORKSPACE_UPDATED,
                org_id=org_id,
                target_type=TARGET_WORKSPACE,
                target_id=workspace_id,
                metadata={"from_name": row.name, "to_name": body.name},
            )
            row = replace(row, name=body.name)
    return access.view(row)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workspace(tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID) -> Response:
    with tenant.transaction(org_id) as session:
        access = _workspace_access(tenant, session, org_id, workspace_id, Action.WORKSPACE_DELETE)
        service.soft_delete_workspace(session, org_id, workspace_id)
        _audit(
            tenant,
            session,
            AuditEventType.WORKSPACE_DELETED,
            org_id=org_id,
            target_type=TARGET_WORKSPACE,
            target_id=workspace_id,
            metadata={"slug": access.workspace.slug},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{workspace_id}/members/{user_id}", response_model=WorkspaceMember)
def put_workspace_member(
    tenant: Tenant,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    body: WorkspaceMemberRoleIn,
) -> WorkspaceMember:
    """Add an org member to the workspace or change their role; non-org-members -> 422."""
    with tenant.transaction(org_id) as session:
        _workspace_access(tenant, session, org_id, workspace_id, Action.WORKSPACE_MEMBERS_MANAGE)
        user = service.org_user(session, org_id, user_id)
        if user is None:
            raise api_error(status.HTTP_422_UNPROCESSABLE_CONTENT, NOT_ORG_MEMBER)
        email, display_name = user
        current = service.member_role(session, org_id, workspace_id, user_id)
        if current is None:
            service.add_member(session, org_id, workspace_id, user_id, body.role)
        elif current != body.role:
            service.change_member_role(session, org_id, workspace_id, user_id, body.role)
        if current != body.role:
            _audit(
                tenant,
                session,
                AuditEventType.WORKSPACE_MEMBER_ADDED
                if current is None
                else AuditEventType.WORKSPACE_MEMBER_ROLE_CHANGED,
                org_id=org_id,
                target_type=TARGET_USER,
                target_id=user_id,
                metadata={
                    "workspace_id": str(workspace_id),
                    "email": email,
                    "from_role": current.value if current is not None else None,
                    "to_role": body.role.value,
                },
            )
    return WorkspaceMember(user_id=user_id, email=email, display_name=display_name, role=body.role)


@router.delete("/{workspace_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workspace_member(
    tenant: Tenant, org_id: uuid.UUID, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> Response:
    with tenant.transaction(org_id) as session:
        _workspace_access(tenant, session, org_id, workspace_id, Action.WORKSPACE_MEMBERS_MANAGE)
        current = service.member_role(session, org_id, workspace_id, user_id)
        if current is None:
            raise not_found()
        user = service.org_user(session, org_id, user_id)
        service.remove_member(session, org_id, workspace_id, user_id)
        _audit(
            tenant,
            session,
            AuditEventType.WORKSPACE_MEMBER_REMOVED,
            org_id=org_id,
            target_type=TARGET_USER,
            target_id=user_id,
            metadata={
                "workspace_id": str(workspace_id),
                "email": user[0] if user is not None else None,
                "from_role": current.value,
                "to_role": None,
            },
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
