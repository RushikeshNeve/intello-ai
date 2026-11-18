"""Workspace data access (M01.02.04). Every function takes a tenant session for the path org."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import and_, delete, func, insert, select, tuple_, update
from sqlalchemy.orm import Session

from app.api.deps import KeysetCursor, Pagination
from app.core.models import OrganizationMembership, User, WorkspaceMembership
from app.core.models import Workspace as WorkspaceModel
from app.modules.authorization.policy import OrgRole, WorkspaceRole

TARGET_WORKSPACE: Final = "workspace"
TARGET_USER: Final = "user"
_ORG_MANAGERS: Final = frozenset({OrgRole.OWNER, OrgRole.ADMIN})


@dataclass(frozen=True)
class WorkspaceRow:
    id: uuid.UUID
    org_id: uuid.UUID
    slug: str
    name: str
    created_at: datetime


@dataclass(frozen=True)
class WorkspaceMemberRow:
    user_id: uuid.UUID
    email: str | None
    display_name: str | None
    role: WorkspaceRole


def effective_role(org_role: OrgRole | None, workspace_role: WorkspaceRole | None) -> WorkspaceRole:
    """Org owners/admins act as workspace admins; otherwise the explicit workspace role."""
    if org_role in _ORG_MANAGERS:
        return WorkspaceRole.ADMIN
    assert workspace_role is not None  # noqa: S101 - callers only ask for readable workspaces
    return workspace_role


def load_workspace(
    session: Session, org_id: uuid.UUID, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[WorkspaceRow, WorkspaceRole | None] | None:
    """The live workspace in the path org and the caller's explicit role (``None`` = none)."""
    row = session.execute(
        select(
            WorkspaceModel.id,
            WorkspaceModel.org_id,
            WorkspaceModel.slug,
            WorkspaceModel.name,
            WorkspaceModel.created_at,
        ).where(
            WorkspaceModel.id == workspace_id,
            WorkspaceModel.org_id == org_id,
            WorkspaceModel.deleted_at.is_(None),
        )
    ).one_or_none()
    if row is None:
        return None
    role = session.execute(
        select(WorkspaceMembership.role).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.org_id == org_id,
            WorkspaceMembership.user_id == user_id,
        )
    ).scalar_one_or_none()
    workspace = WorkspaceRow(
        id=row.id, org_id=row.org_id, slug=row.slug, name=row.name, created_at=row.created_at
    )
    return workspace, (WorkspaceRole(role) if role is not None else None)


def list_readable(
    session: Session,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    org_role: OrgRole,
    page: Pagination,
) -> tuple[list[tuple[WorkspaceRow, WorkspaceRole]], str | None]:
    """Live workspaces the caller may read: all for org owners/admins, else their memberships."""
    stmt = (
        select(
            WorkspaceModel.id,
            WorkspaceModel.org_id,
            WorkspaceModel.slug,
            WorkspaceModel.name,
            WorkspaceModel.created_at,
            WorkspaceMembership.role,
        )
        .where(WorkspaceModel.org_id == org_id, WorkspaceModel.deleted_at.is_(None))
        .order_by(WorkspaceModel.created_at, WorkspaceModel.id)
        .limit(page.limit + 1)
    )
    on = and_(
        WorkspaceMembership.workspace_id == WorkspaceModel.id,
        WorkspaceMembership.org_id == WorkspaceModel.org_id,
        WorkspaceMembership.user_id == user_id,
    )
    stmt = (
        stmt.outerjoin(WorkspaceMembership, on)
        if org_role in _ORG_MANAGERS
        else stmt.join(WorkspaceMembership, on)
    )
    if page.after is not None:
        stmt = stmt.where(
            tuple_(WorkspaceModel.created_at, WorkspaceModel.id)
            > tuple_(page.after.created_at, page.after.id)
        )
    rows = session.execute(stmt).all()
    items = [
        (
            WorkspaceRow(
                id=r.id, org_id=r.org_id, slug=r.slug, name=r.name, created_at=r.created_at
            ),
            effective_role(org_role, WorkspaceRole(r.role) if r.role is not None else None),
        )
        for r in rows[: page.limit]
    ]
    next_cursor = None
    if len(rows) > page.limit:
        last = items[-1][0]
        next_cursor = KeysetCursor(created_at=last.created_at, id=last.id).encode()
    return items, next_cursor


def create_workspace(
    session: Session, *, org_id: uuid.UUID, slug: str, name: str, user_id: uuid.UUID
) -> WorkspaceRow:
    """Insert the workspace and make the creator its admin."""
    workspace_id = uuid.uuid4()
    session.execute(
        insert(WorkspaceModel).values(
            id=workspace_id, org_id=org_id, slug=slug, name=name, created_by=user_id
        )
    )
    session.execute(
        insert(WorkspaceMembership).values(
            workspace_id=workspace_id,
            org_id=org_id,
            user_id=user_id,
            role=WorkspaceRole.ADMIN.value,
        )
    )
    created_at = session.execute(
        select(WorkspaceModel.created_at).where(WorkspaceModel.id == workspace_id)
    ).scalar_one()
    return WorkspaceRow(id=workspace_id, org_id=org_id, slug=slug, name=name, created_at=created_at)


def rename_workspace(
    session: Session, org_id: uuid.UUID, workspace_id: uuid.UUID, name: str
) -> None:
    session.execute(
        update(WorkspaceModel)
        .where(WorkspaceModel.id == workspace_id, WorkspaceModel.org_id == org_id)
        .values(name=name)
        .execution_options(synchronize_session=False)
    )


def soft_delete_workspace(session: Session, org_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    session.execute(
        update(WorkspaceModel)
        .where(
            WorkspaceModel.id == workspace_id,
            WorkspaceModel.org_id == org_id,
            WorkspaceModel.deleted_at.is_(None),
        )
        .values(deleted_at=func.now())
        .execution_options(synchronize_session=False)
    )


def org_user(
    session: Session, org_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[str | None, str | None] | None:
    """(email, display_name) of an org member; ``None`` when the user is not in the org."""
    row = session.execute(
        select(User.email, User.display_name)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .where(OrganizationMembership.org_id == org_id, User.id == user_id)
    ).one_or_none()
    return (row.email, row.display_name) if row is not None else None


def member_role(
    session: Session, org_id: uuid.UUID, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> WorkspaceRole | None:
    role = session.execute(
        select(WorkspaceMembership.role)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.org_id == org_id,
            WorkspaceMembership.user_id == user_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    return WorkspaceRole(role) if role is not None else None


def add_member(
    session: Session,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    role: WorkspaceRole,
) -> None:
    session.execute(
        insert(WorkspaceMembership).values(
            workspace_id=workspace_id, org_id=org_id, user_id=user_id, role=role.value
        )
    )


def change_member_role(
    session: Session,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    role: WorkspaceRole,
) -> None:
    session.execute(
        update(WorkspaceMembership)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.org_id == org_id,
            WorkspaceMembership.user_id == user_id,
        )
        .values(role=role.value)
        .execution_options(synchronize_session=False)
    )


def remove_member(
    session: Session, org_id: uuid.UUID, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    session.execute(
        delete(WorkspaceMembership)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.org_id == org_id,
            WorkspaceMembership.user_id == user_id,
        )
        .execution_options(synchronize_session=False)
    )
