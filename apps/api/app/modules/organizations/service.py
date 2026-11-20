"""Organization data access and rules (M01.02.02). Every function takes a tenant session.

The caller opens the session with ``TenantRequest.transaction(org_id)``, so RLS sees the path org
(``app.current_org_id``) and the principal (``app.current_user_id``). Queries also filter by the
path org explicitly; ids from anywhere but the path are never trusted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import Row, Select, and_, delete, func, insert, select, text, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import AuthzDenied, KeysetCursor, Pagination
from app.core.models import Organization, OrganizationMembership, OrgMemberPreapproval, User
from app.modules.authorization.policy import (
    Action,
    Decision,
    OrgRole,
    SubjectContext,
    can_assign_org_role,
    decide,
)

TARGET_ORG: Final = "organization"
TARGET_USER: Final = "user"
FK_MEMBERSHIP_USER: Final = "fk_organization_memberships_user_id"
PK_MEMBERSHIP: Final = "pk_organization_memberships"


@dataclass(frozen=True)
class OrgRow:
    id: uuid.UUID
    slug: str
    name: str
    created_at: datetime


@dataclass(frozen=True)
class OrgAccess:
    """A live org from the path plus the caller's role in it (``None`` = not a member)."""

    org: OrgRow
    subject: SubjectContext

    @property
    def role(self) -> OrgRole | None:
        return self.subject.org_role

    def require(
        self, action: Action, *, target_type: str = TARGET_ORG, target_id: str = ""
    ) -> None:
        """``decide()``; raise :class:`AuthzDenied` (audited 404/403) when it denies."""
        decision = decide(self.subject, action)
        if not decision.allowed:
            raise self.denied(decision, action, target_type=target_type, target_id=target_id)

    def denied(
        self, decision: Decision, action: Action, *, target_type: str, target_id: str
    ) -> AuthzDenied:
        return AuthzDenied(
            decision,
            action=action.value,
            org_id=self.org.id,
            target_type=target_type,
            target_id=target_id or str(self.org.id),
        )


@dataclass(frozen=True)
class MemberRow:
    user_id: uuid.UUID
    email: str | None
    display_name: str | None
    role: OrgRole
    created_at: datetime


class UserNotFoundError(Exception):
    """PUT member for a user id that does not exist."""


class MembershipConflictError(Exception):
    """A concurrent request created the same membership first."""


def load_org_access(
    session: Session, org_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False
) -> OrgAccess | None:
    """The live org ``org_id`` and the caller's role; ``None`` if missing or soft-deleted.

    ``lock=True`` takes ``FOR UPDATE`` on the organization row. Membership changes use it to
    serialize per org: the deferred "has an owner" trigger alone can't stop two concurrent
    transactions from each demoting the other last owner under READ COMMITTED.
    """
    stmt = select(Organization.id, Organization.slug, Organization.name, Organization.created_at)
    stmt = stmt.where(Organization.id == org_id, Organization.deleted_at.is_(None))
    if lock:
        stmt = stmt.with_for_update()
    row = session.execute(stmt).one_or_none()
    if row is None:
        return None
    role = session.execute(
        select(OrganizationMembership.role).where(
            OrganizationMembership.org_id == org_id, OrganizationMembership.user_id == user_id
        )
    ).scalar_one_or_none()
    org = OrgRow(id=row.id, slug=row.slug, name=row.name, created_at=row.created_at)
    subject = SubjectContext(
        user_id=user_id,
        org_role=OrgRole(role) if role is not None else None,
        workspace_role=None,
    )
    return OrgAccess(org=org, subject=subject)


def list_my_orgs(
    session: Session, user_id: uuid.UUID, page: Pagination
) -> tuple[list[tuple[OrgRow, OrgRole]], str | None]:
    stmt = (
        select(
            Organization.id,
            Organization.slug,
            Organization.name,
            Organization.created_at,
            OrganizationMembership.role,
        )
        .join(
            OrganizationMembership,
            and_(
                OrganizationMembership.org_id == Organization.id,
                OrganizationMembership.user_id == user_id,
            ),
        )
        .where(Organization.deleted_at.is_(None))
        .order_by(Organization.created_at, Organization.id)
        .limit(page.limit + 1)
    )
    if page.after is not None:
        stmt = stmt.where(
            tuple_(Organization.created_at, Organization.id)
            > tuple_(page.after.created_at, page.after.id)
        )
    rows = session.execute(stmt).all()
    items = [
        (OrgRow(id=r.id, slug=r.slug, name=r.name, created_at=r.created_at), OrgRole(r.role))
        for r in rows[: page.limit]
    ]
    next_cursor = None
    if len(rows) > page.limit:
        last = items[-1][0]
        next_cursor = KeysetCursor(created_at=last.created_at, id=last.id).encode()
    return items, next_cursor


def create_org(
    session: Session, *, org_id: uuid.UUID, slug: str, name: str, user_id: uuid.UUID
) -> OrgRow:
    """Insert the org and the creator's owner membership (the owner check runs at COMMIT)."""
    session.execute(
        insert(Organization).values(id=org_id, slug=slug, name=name, created_by=user_id)
    )
    session.execute(
        insert(OrganizationMembership).values(
            org_id=org_id, user_id=user_id, role=OrgRole.OWNER.value
        )
    )
    created_at = session.execute(
        select(Organization.created_at).where(Organization.id == org_id)
    ).scalar_one()
    return OrgRow(id=org_id, slug=slug, name=name, created_at=created_at)


def rename_org(session: Session, org_id: uuid.UUID, name: str) -> None:
    session.execute(
        update(Organization)
        .where(Organization.id == org_id)
        .values(name=name)
        .execution_options(synchronize_session=False)
    )


def soft_delete_org(session: Session, org_id: uuid.UUID) -> None:
    session.execute(
        update(Organization)
        .where(Organization.id == org_id, Organization.deleted_at.is_(None))
        .values(deleted_at=func.now())
        .execution_options(synchronize_session=False)
    )


def _member_select(
    org_id: uuid.UUID,
) -> Select[uuid.UUID, str | None, str | None, str, datetime]:
    return (
        select(
            OrganizationMembership.user_id,
            User.email,
            User.display_name,
            OrganizationMembership.role,
            OrganizationMembership.created_at,
        )
        .join(User, User.id == OrganizationMembership.user_id)
        .where(OrganizationMembership.org_id == org_id)
    )


def _member(row: Row[uuid.UUID, str | None, str | None, str, datetime]) -> MemberRow:
    user_id, email, display_name, role, created_at = row
    return MemberRow(
        user_id=user_id,
        email=email,
        display_name=display_name,
        role=OrgRole(role),
        created_at=created_at,
    )


def list_members(
    session: Session, org_id: uuid.UUID, page: Pagination
) -> tuple[list[MemberRow], str | None]:
    stmt = (
        _member_select(org_id)
        .order_by(OrganizationMembership.created_at, OrganizationMembership.user_id)
        .limit(page.limit + 1)
    )
    if page.after is not None:
        stmt = stmt.where(
            tuple_(OrganizationMembership.created_at, OrganizationMembership.user_id)
            > tuple_(page.after.created_at, page.after.id)
        )
    rows = [_member(r) for r in session.execute(stmt).all()]
    items = rows[: page.limit]
    next_cursor = None
    if len(rows) > page.limit:
        last = items[-1]
        next_cursor = KeysetCursor(created_at=last.created_at, id=last.user_id).encode()
    return items, next_cursor


def get_member(session: Session, org_id: uuid.UUID, user_id: uuid.UUID) -> MemberRow | None:
    row = session.execute(
        _member_select(org_id).where(OrganizationMembership.user_id == user_id)
    ).one_or_none()
    return _member(row) if row is not None else None


def current_member_role(session: Session, org_id: uuid.UUID, user_id: uuid.UUID) -> OrgRole | None:
    role = session.execute(
        select(OrganizationMembership.role)
        .where(OrganizationMembership.org_id == org_id, OrganizationMembership.user_id == user_id)
        .with_for_update()
    ).scalar_one_or_none()
    return OrgRole(role) if role is not None else None


def check_role_change(
    access: OrgAccess,
    target_user_id: uuid.UUID,
    current: OrgRole | None,
    new: OrgRole | None,
) -> None:
    """``org.members.manage`` then the owner-only rule; raise :class:`AuthzDenied` on deny."""
    access.require(
        Action.ORG_MEMBERS_MANAGE, target_type=TARGET_USER, target_id=str(target_user_id)
    )
    decision = can_assign_org_role(access.subject, current, new)
    if not decision.allowed:
        raise access.denied(
            decision,
            Action.ORG_MEMBERS_MANAGE,
            target_type=TARGET_USER,
            target_id=str(target_user_id),
        )


def add_member(session: Session, org_id: uuid.UUID, user_id: uuid.UUID, role: OrgRole) -> None:
    try:
        with session.begin_nested():
            session.execute(
                insert(OrganizationMembership).values(
                    org_id=org_id, user_id=user_id, role=role.value
                )
            )
    except IntegrityError as exc:
        name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if name == FK_MEMBERSHIP_USER:
            raise UserNotFoundError from None
        if name == PK_MEMBERSHIP:
            raise MembershipConflictError from None
        raise


def change_member_role(
    session: Session, org_id: uuid.UUID, user_id: uuid.UUID, role: OrgRole
) -> None:
    session.execute(
        update(OrganizationMembership)
        .where(OrganizationMembership.org_id == org_id, OrganizationMembership.user_id == user_id)
        .values(role=role.value)
        .execution_options(synchronize_session=False)
    )


def remove_member(session: Session, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Delete the membership; its workspace memberships go with it (FK ON DELETE CASCADE)."""
    session.execute(
        delete(OrganizationMembership)
        .where(OrganizationMembership.org_id == org_id, OrganizationMembership.user_id == user_id)
        .execution_options(synchronize_session=False)
    )


@dataclass(frozen=True)
class PreapprovalRow:
    id: uuid.UUID
    email: str
    role: OrgRole
    created_at: datetime


def find_verified_user(session: Session, email: str) -> uuid.UUID | None:
    """The one user with this VERIFIED email (definer: ``users`` RLS hides other orgs' users)."""
    found = session.execute(text("SELECT preapproval_find_user(:e)"), {"e": email}).scalar_one()
    return uuid.UUID(str(found)) if found is not None else None


def list_preapprovals(session: Session, org_id: uuid.UUID) -> list[PreapprovalRow]:
    rows = session.execute(
        select(
            OrgMemberPreapproval.id,
            OrgMemberPreapproval.email,
            OrgMemberPreapproval.role,
            OrgMemberPreapproval.created_at,
        )
        .where(OrgMemberPreapproval.org_id == org_id, OrgMemberPreapproval.consumed_at.is_(None))
        .order_by(OrgMemberPreapproval.created_at, OrgMemberPreapproval.id)
    ).all()
    return [PreapprovalRow(r.id, r.email, OrgRole(r.role), r.created_at) for r in rows]


def get_preapproval(
    session: Session, org_id: uuid.UUID, preapproval_id: uuid.UUID
) -> PreapprovalRow | None:
    return next((p for p in list_preapprovals(session, org_id) if p.id == preapproval_id), None)


def upsert_preapproval(
    session: Session, *, org_id: uuid.UUID, email: str, role: OrgRole, created_by: uuid.UUID
) -> PreapprovalRow:
    """One pending entry per (org, email): a repeat replaces the role."""
    existing = next((p for p in list_preapprovals(session, org_id) if p.email == email), None)
    if existing is not None:
        delete_preapproval(session, org_id, existing.id)
    new_id = uuid.uuid4()
    session.execute(
        insert(OrgMemberPreapproval).values(
            id=new_id, org_id=org_id, email=email, role=role.value, created_by=created_by
        )
    )
    row = get_preapproval(session, org_id, new_id)
    assert row is not None  # noqa: S101 - just inserted in this transaction
    return row


def delete_preapproval(session: Session, org_id: uuid.UUID, preapproval_id: uuid.UUID) -> None:
    session.execute(
        delete(OrgMemberPreapproval).where(
            OrgMemberPreapproval.id == preapproval_id, OrgMemberPreapproval.org_id == org_id
        )
    )
