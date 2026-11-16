"""ORM models for the M01 identity/tenancy schema (contract docs/architecture/m01-contracts.md §2).

The schema is owned by Alembic (``migrations/versions/0001_identity_tenancy.py``); these models
mirror it, including constraint names, so callers can map ``IntegrityError.diag.constraint_name``
to stable API error codes. Ids are UUIDv4 generated in the application; timestamps are UTC
``timestamptz``. Row-level security, the "every live org has an owner" deferred trigger and the
append-only audit trigger exist only in the migration (they are not expressible as ORM metadata).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ORG_ROLES: Final = ("owner", "admin", "member")
WORKSPACE_ROLES: Final = ("admin", "editor", "viewer")
GRANT_PERMISSIONS: Final = ("read", "write", "manage")
GRANT_RESOURCE_TYPES: Final = ("document",)
GRANTEE_TYPES: Final = ("user", "workspace_role")
AUDIT_OUTCOMES: Final = ("success", "denied", "error")

SLUG_PATTERN: Final = "^[a-z0-9][a-z0-9-]{1,62}$"
UUID_TEXT_PATTERN: Final = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

# Constraint names the API maps to error codes.
ORG_SLUG_UNIQUE: Final = "uq_organizations_slug"
WORKSPACE_SLUG_UNIQUE: Final = "uq_workspaces_org_id_slug"
ORG_OWNER_CONSTRAINT: Final = "organization_has_owner"  # raised by the deferred trigger
WORKSPACE_MEMBER_ORG_FK: Final = "fk_workspace_memberships_org_member"


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _tstz() -> TIMESTAMP:
    return TIMESTAMP(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_users_issuer_subject"),
        CheckConstraint("length(issuer) > 0", name="ck_users_issuer_not_empty"),
        CheckConstraint("length(subject) > 0", name="ck_users_subject_not_empty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    issuer: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("slug", name=ORG_SLUG_UNIQUE),
        # Lowercase-only pattern: uniqueness on the stored value is case-insensitive.
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="ck_organizations_slug_format"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_organizations_name_not_empty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_organizations_created_by")
    )
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(_tstz())


class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        PrimaryKeyConstraint("org_id", "user_id", name="pk_organization_memberships"),
        CheckConstraint(_in("role", ORG_ROLES), name="ck_organization_memberships_role"),
        Index("ix_organization_memberships_user_id", "user_id"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "organizations.id", name="fk_organization_memberships_org_id", ondelete="CASCADE"
        ),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_organization_memberships_user_id")
    )
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("org_id", "slug", name=WORKSPACE_SLUG_UNIQUE),
        # Target of the composite FKs that pin workspace_memberships/resource_grants.org_id.
        UniqueConstraint("id", "org_id", name="uq_workspaces_id_org_id"),
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="ck_workspaces_slug_format"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_workspaces_name_not_empty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_workspaces_org_id", ondelete="CASCADE"),
    )
    slug: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_workspaces_created_by")
    )
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(_tstz())


class WorkspaceMembership(Base):
    __tablename__ = "workspace_memberships"
    __table_args__ = (
        PrimaryKeyConstraint("workspace_id", "user_id", name="pk_workspace_memberships"),
        CheckConstraint(_in("role", WORKSPACE_ROLES), name="ck_workspace_memberships_role"),
        # org_id must be the workspace's org.
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_workspace_memberships_workspace",
            ondelete="CASCADE",
        ),
        # The user must be a member of that org; removing the org membership removes these.
        ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["organization_memberships.org_id", "organization_memberships.user_id"],
            name=WORKSPACE_MEMBER_ORG_FK,
            ondelete="CASCADE",
        ),
        Index("ix_workspace_memberships_org_id_user_id", "org_id", "user_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_workspace_memberships_user_id")
    )
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())


class ResourceGrant(Base):
    __tablename__ = "resource_grants"
    __table_args__ = (
        CheckConstraint(
            _in("resource_type", GRANT_RESOURCE_TYPES), name="ck_resource_grants_resource_type"
        ),
        CheckConstraint(_in("grantee_type", GRANTEE_TYPES), name="ck_resource_grants_grantee_type"),
        CheckConstraint(
            f"(grantee_type = 'user' AND grantee_id ~ '{UUID_TEXT_PATTERN}')"
            f" OR (grantee_type = 'workspace_role' AND {_in('grantee_id', WORKSPACE_ROLES)})",
            name="ck_resource_grants_grantee_id",
        ),
        CheckConstraint(_in("permission", GRANT_PERMISSIONS), name="ck_resource_grants_permission"),
        CheckConstraint(
            "revoked_at IS NOT NULL OR revoked_by IS NULL", name="ck_resource_grants_revoked"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_resource_grants_workspace",
            ondelete="CASCADE",
        ),
        Index("ix_resource_grants_resource", "org_id", "resource_type", "resource_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_resource_grants_org_id", ondelete="CASCADE"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    resource_type: Mapped[str] = mapped_column(Text)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    grantee_type: Mapped[str] = mapped_column(Text)
    grantee_id: Mapped[str] = mapped_column(Text)
    permission: Mapped[str] = mapped_column(Text)
    granted_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_resource_grants_granted_by")
    )
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(_tstz())
    revoked_at: Mapped[datetime | None] = mapped_column(_tstz())
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_resource_grants_revoked_by")
    )


class AuditEvent(Base):
    """Append-only. No FKs on org_id/actor_user_id so a denial for an unknown id can be recorded."""

    __tablename__ = "audit_events"
    # INSERT ... RETURNING would require the new row to pass the SELECT policy, which org-less
    # events (org_id NULL) never do. Server defaults are not fetched back.
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012
    __table_args__ = (
        CheckConstraint(_in("outcome", AUDIT_OUTCOMES), name="ck_audit_events_outcome"),
        CheckConstraint("length(event_type) > 0", name="ck_audit_events_event_type_not_empty"),
        Index("ix_audit_events_org_id_occurred_at", "org_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )


ORG_SCOPED_TABLES: Final = (
    "organizations",
    "organization_memberships",
    "workspaces",
    "workspace_memberships",
    "resource_grants",
    "audit_events",
)
RLS_TABLES: Final = ("users", *ORG_SCOPED_TABLES)
