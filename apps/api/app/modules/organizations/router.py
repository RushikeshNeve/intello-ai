"""Organization REST endpoints (M01.02.02, contract m01-contracts.md §3-§5).

Per request: principal -> tenant transaction (user + path org) -> caller's org role ->
``decide()``. A denial is audited in its own transaction and answered 404 ``not_found`` (caller is
not a member: existence is never revealed) or 403 ``forbidden``. Missing or soft-deleted orgs are
404. Every successful change writes its audit event in the same transaction.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status
from sqlalchemy.orm import Session

from app.api.deps import (
    CONFLICT,
    USER_NOT_FOUND,
    Page,
    Tenant,
    TenantRequest,
    api_error,
    not_found,
)
from app.modules.audit import AuditEventType, record_event
from app.modules.authorization.policy import Action, OrgRole
from app.modules.organizations import service
from app.modules.organizations.schemas import (
    Member,
    MemberPage,
    MemberRoleIn,
    Org,
    OrgCreate,
    OrgPage,
    OrgSummary,
    OrgUpdate,
)
from app.modules.organizations.service import TARGET_ORG, TARGET_USER, OrgAccess, OrgRow

router = APIRouter(prefix="/v1/orgs", tags=["organizations"])


def _org(row: OrgRow, access: OrgAccess) -> Org:
    assert access.role is not None  # noqa: S101 - only called after an allowed decision
    return Org(id=row.id, slug=row.slug, name=row.name, created_at=row.created_at, role=access.role)


def _access(tenant: TenantRequest, session: Session, org_id: uuid.UUID) -> OrgAccess:
    access = service.load_org_access(session, org_id, tenant.user_id)
    if access is None:
        raise not_found()
    return access


def _member(row: service.MemberRow) -> Member:
    return Member(
        user_id=row.user_id, email=row.email, display_name=row.display_name, role=row.role
    )


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


@router.get("", response_model=OrgPage)
def list_orgs(tenant: Tenant, page: Page) -> OrgPage:
    """The caller's live organizations (no org context: RLS shows only their own memberships)."""
    with tenant.transaction(None) as session:
        items, next_cursor = service.list_my_orgs(session, tenant.user_id, page)
    return OrgPage(
        items=[OrgSummary(id=o.id, slug=o.slug, name=o.name, role=role) for o, role in items],
        next_cursor=next_cursor,
    )


@router.post("", response_model=Org, status_code=status.HTTP_201_CREATED)
def create_org(tenant: Tenant, body: OrgCreate) -> Org:
    """Any authenticated user; the creator becomes owner in the same transaction."""
    org_id = uuid.uuid4()
    with tenant.transaction(org_id) as session:
        row = service.create_org(
            session, org_id=org_id, slug=body.slug, name=body.name, user_id=tenant.user_id
        )
        _audit(
            tenant,
            session,
            AuditEventType.ORG_CREATED,
            org_id=org_id,
            target_type=TARGET_ORG,
            target_id=org_id,
            metadata={"slug": row.slug, "name": row.name},
        )
    return Org(
        id=row.id, slug=row.slug, name=row.name, created_at=row.created_at, role=OrgRole.OWNER
    )


@router.get("/{org_id}", response_model=Org)
def get_org(tenant: Tenant, org_id: uuid.UUID) -> Org:
    with tenant.transaction(org_id) as session:
        access = _access(tenant, session, org_id)
        access.require(Action.ORG_READ)
    return _org(access.org, access)


@router.patch("/{org_id}", response_model=Org)
def update_org(tenant: Tenant, org_id: uuid.UUID, body: OrgUpdate) -> Org:
    with tenant.transaction(org_id) as session:
        access = _access(tenant, session, org_id)
        access.require(Action.ORG_UPDATE)
        row = access.org
        if body.name is not None and body.name != row.name:
            service.rename_org(session, org_id, body.name)
            _audit(
                tenant,
                session,
                AuditEventType.ORG_UPDATED,
                org_id=org_id,
                target_type=TARGET_ORG,
                target_id=org_id,
                metadata={"from_name": row.name, "to_name": body.name},
            )
            row = OrgRow(id=row.id, slug=row.slug, name=body.name, created_at=row.created_at)
    return _org(row, access)


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_org(tenant: Tenant, org_id: uuid.UUID) -> Response:
    """Soft delete (owner only). Afterwards every route answers 404 for this org."""
    with tenant.transaction(org_id) as session:
        access = _access(tenant, session, org_id)
        access.require(Action.ORG_DELETE)
        service.soft_delete_org(session, org_id)
        _audit(
            tenant,
            session,
            AuditEventType.ORG_DELETED,
            org_id=org_id,
            target_type=TARGET_ORG,
            target_id=org_id,
            metadata={"slug": access.org.slug},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{org_id}/members", response_model=MemberPage)
def list_members(tenant: Tenant, org_id: uuid.UUID, page: Page) -> MemberPage:
    with tenant.transaction(org_id) as session:
        access = _access(tenant, session, org_id)
        access.require(Action.ORG_MEMBERS_READ)
        items, next_cursor = service.list_members(session, org_id, page)
    return MemberPage(items=[_member(m) for m in items], next_cursor=next_cursor)


@router.put("/{org_id}/members/{user_id}", response_model=Member)
def put_member(tenant: Tenant, org_id: uuid.UUID, user_id: uuid.UUID, body: MemberRoleIn) -> Member:
    """Add an existing user or change their role. Only owners grant or revoke ``owner``.

    Demoting the last owner fails at COMMIT (deferred trigger) -> 409 ``last_owner``.
    """
    with tenant.transaction(org_id) as session:
        access = service.load_org_access(session, org_id, tenant.user_id, lock=True)
        if access is None:
            raise not_found()
        access.require(Action.ORG_MEMBERS_MANAGE, target_type=TARGET_USER, target_id=str(user_id))
        current = service.current_member_role(session, org_id, user_id)
        service.check_role_change(access, user_id, current, body.role)
        if current is None:
            try:
                service.add_member(session, org_id, user_id, body.role)
            except service.UserNotFoundError:
                raise api_error(status.HTTP_404_NOT_FOUND, USER_NOT_FOUND) from None
            except service.MembershipConflictError:
                raise api_error(status.HTTP_409_CONFLICT, CONFLICT) from None
        elif current != body.role:
            service.change_member_role(session, org_id, user_id, body.role)
        member = service.get_member(session, org_id, user_id)
        assert member is not None  # noqa: S101 - just inserted/updated in this transaction
        if current != body.role:
            _audit(
                tenant,
                session,
                AuditEventType.ORG_MEMBER_ADDED
                if current is None
                else AuditEventType.ORG_MEMBER_ROLE_CHANGED,
                org_id=org_id,
                target_type=TARGET_USER,
                target_id=user_id,
                metadata={
                    "email": member.email,
                    "from_role": current.value if current is not None else None,
                    "to_role": body.role.value,
                },
            )
    return _member(member)


@router.delete("/{org_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_member(tenant: Tenant, org_id: uuid.UUID, user_id: uuid.UUID) -> Response:
    """Remove a member (and their workspace memberships). The last owner -> 409 ``last_owner``."""
    with tenant.transaction(org_id) as session:
        access = service.load_org_access(session, org_id, tenant.user_id, lock=True)
        if access is None:
            raise not_found()
        access.require(Action.ORG_MEMBERS_MANAGE, target_type=TARGET_USER, target_id=str(user_id))
        member = service.get_member(session, org_id, user_id)
        if member is None:
            raise not_found()
        current = service.current_member_role(session, org_id, user_id)
        service.check_role_change(access, user_id, current, None)
        service.remove_member(session, org_id, user_id)
        _audit(
            tenant,
            session,
            AuditEventType.ORG_MEMBER_REMOVED,
            org_id=org_id,
            target_type=TARGET_USER,
            target_id=user_id,
            metadata={"email": member.email, "from_role": member.role.value, "to_role": None},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
