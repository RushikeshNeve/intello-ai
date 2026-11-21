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
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
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

# M02 (docs/architecture/m02-contracts.md §3, §6).
SOURCE_KINDS: Final = ("upload", "url", "connector")
SOURCE_KIND_UPLOAD: Final = "upload"
UPLOAD_STATES: Final = ("pending", "finalized", "failed", "expired")
UPLOAD_STATE_PENDING: Final = "pending"
UPLOAD_STATE_FINALIZED: Final = "finalized"
UPLOAD_STATE_FAILED: Final = "failed"
UPLOAD_STATE_EXPIRED: Final = "expired"
TITLE_MAX_LENGTH: Final = 255
FILENAME_MAX_LENGTH: Final = 255
CONTENT_TYPE_MAX_LENGTH: Final = 255
SHA256_PATTERN: Final = "^[0-9a-f]{64}$"
# Only these types may be served with ``Content-Disposition: inline``; everything else
# (text/html, image/svg+xml, scripts, unknown types) is always an attachment (contract §6).
INLINE_CONTENT_TYPES: Final = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }
)

SLUG_PATTERN: Final = "^[a-z0-9][a-z0-9-]{1,62}$"
UUID_TEXT_PATTERN: Final = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

# Constraint names the API maps to error codes.
ORG_SLUG_UNIQUE: Final = "uq_organizations_slug"
WORKSPACE_SLUG_UNIQUE: Final = "uq_workspaces_org_id_slug"
ORG_OWNER_CONSTRAINT: Final = "organization_has_owner"  # raised by the deferred trigger
WORKSPACE_MEMBER_ORG_FK: Final = "fk_workspace_memberships_org_member"
UPLOAD_SOURCE_UNIQUE: Final = "uq_sources_workspace_id_upload"
DOCUMENT_VERSION_NUMBER_UNIQUE: Final = "uq_document_versions_document_id_version_number"
DOCUMENT_VERSION_OBJECT_KEY_UNIQUE: Final = "uq_document_versions_object_key"
UPLOAD_SESSION_VERSION_UNIQUE: Final = "uq_upload_sessions_version_id"
ACTIVE_GRANT_UNIQUE: Final = "uq_resource_grants_active"
# Raised by triggers (not expressible as ORM metadata).
VERSION_IMMUTABLE_CONSTRAINT: Final = "document_versions_immutable"  # SQLSTATE 42501
UPLOAD_STATE_FORWARD_CONSTRAINT: Final = "upload_sessions_state_forward_only"  # SQLSTATE 23514


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
    # From the OIDC ``email_verified`` claim (migration 0011); pre-approvals match only when true.
    email_verified: Mapped[bool] = mapped_column(server_default=text("false"))


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


class OrgMemberPreapproval(Base):
    """Access granted by email before the person's first sign-in (migration 0011)."""

    __tablename__ = "org_member_preapprovals"
    __table_args__ = (
        CheckConstraint(
            "email = lower(email) AND length(email) > 3", name="ck_org_member_preapprovals_email"
        ),
        CheckConstraint(_in("role", ORG_ROLES), name="ck_org_member_preapprovals_role"),
        Index(
            "uq_org_member_preapprovals_pending",
            "org_id",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("consumed_at IS NULL"),
        ),
        Index(
            "ix_org_member_preapprovals_email_pending",
            text("lower(email)"),
            postgresql_where=text("consumed_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "organizations.id", name="fk_org_member_preapprovals_org_id", ondelete="CASCADE"
        ),
    )
    email: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_org_member_preapprovals_created_by")
    )
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
    consumed_at: Mapped[datetime | None] = mapped_column(_tstz())


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
        # One ACTIVE grant per (resource, grantee, permission); revoking frees the slot.
        Index(
            ACTIVE_GRANT_UNIQUE,
            "resource_type",
            "resource_id",
            "grantee_type",
            "grantee_id",
            "permission",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
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


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="uq_sources_id_workspace_id"),
        UniqueConstraint("id", "org_id", name="uq_sources_id_org_id"),
        CheckConstraint(_in("kind", SOURCE_KINDS), name="ck_sources_kind"),
        CheckConstraint("length(name) BETWEEN 1 AND 255", name="ck_sources_name_length"),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_sources_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["connector_account_id", "org_id"],
            ["connector_accounts.id", "connector_accounts.org_id"],
            name="fk_sources_connector_account",
        ),
        Index(
            "ix_sources_next_sync_at",
            "next_sync_at",
            postgresql_where=text(
                "kind = 'connector' AND state = 'active' AND next_sync_at IS NOT NULL"
            ),
        ),
        # Created lazily by the first upload; at most one per workspace.
        Index(
            UPLOAD_SOURCE_UNIQUE,
            "workspace_id",
            unique=True,
            postgresql_where=text("kind = 'upload'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_sources_org_id", ondelete="CASCADE"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_sources_created_by")
    )
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
    # M06 (migration 0009): connector sources. NULL/defaults for upload sources.
    connector_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    sync_cursor: Mapped[str | None] = mapped_column(Text)
    sync_interval_seconds: Mapped[int | None] = mapped_column(Integer)
    next_sync_at: Mapped[datetime | None] = mapped_column(_tstz())
    state: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    revoked_at: Mapped[datetime | None] = mapped_column(_tstz())
    webhook_secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    webhook_key_id: Mapped[str | None] = mapped_column(Text)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="uq_documents_id_workspace_id"),
        CheckConstraint(
            "length(title) BETWEEN 1 AND 255 AND title !~ '^[[:space:]]|[[:space:]]$'",
            name="ck_documents_title",
        ),
        CheckConstraint("(deleted_at IS NULL) = (deleted_by IS NULL)", name="ck_documents_deleted"),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_documents_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id", "workspace_id"],
            ["sources.id", "sources.workspace_id"],
            name="fk_documents_source",
        ),
        # The latest version must belong to this very document. Deferred so the document and its
        # first version can be inserted in either order inside one transaction.
        ForeignKeyConstraint(
            ["latest_version_id", "id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_documents_latest_version",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        Index("ix_documents_workspace_id_updated_at", "workspace_id", "updated_at", "id"),
        Index("ix_documents_workspace_id_created_at", "workspace_id", "created_at", "id"),
        Index("ix_documents_workspace_id_title", "workspace_id", "title", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_documents_org_id", ondelete="CASCADE"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str] = mapped_column(Text)
    latest_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_documents_created_by")
    )
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(_tstz())
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_documents_deleted_by")
    )


class DocumentVersion(Base):
    """Immutable (trigger): only ``purged_at`` may go NULL -> a timestamp."""

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name=DOCUMENT_VERSION_NUMBER_UNIQUE),
        UniqueConstraint("id", "document_id", name="uq_document_versions_id_document_id"),
        UniqueConstraint("object_key", name=DOCUMENT_VERSION_OBJECT_KEY_UNIQUE),
        CheckConstraint("version_number >= 1", name="ck_document_versions_version_number"),
        CheckConstraint(
            "length(original_filename) BETWEEN 1 AND 255",
            name="ck_document_versions_original_filename",
        ),
        CheckConstraint(
            "length(content_type) BETWEEN 1 AND 255", name="ck_document_versions_content_type"
        ),
        CheckConstraint("size_bytes >= 0", name="ck_document_versions_size_bytes"),
        CheckConstraint(f"sha256 ~ '{SHA256_PATTERN}'", name="ck_document_versions_sha256"),
        CheckConstraint("length(object_key) > 0", name="ck_document_versions_object_key"),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_document_versions_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_document_versions_document",
        ),
        Index("ix_document_versions_org_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_document_versions_org_id", ondelete="CASCADE"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_number: Mapped[int] = mapped_column(Integer)
    original_filename: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(Text)
    object_key: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_document_versions_created_by")
    )
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
    purged_at: Mapped[datetime | None] = mapped_column(_tstz())


class UploadSession(Base):
    """``state`` moves forward only (trigger): ``pending`` -> finalized | failed | expired."""

    __tablename__ = "upload_sessions"
    __table_args__ = (
        UniqueConstraint("version_id", name=UPLOAD_SESSION_VERSION_UNIQUE),
        CheckConstraint(_in("state", UPLOAD_STATES), name="ck_upload_sessions_state"),
        CheckConstraint("length(filename) BETWEEN 1 AND 255", name="ck_upload_sessions_filename"),
        CheckConstraint(
            "title IS NULL OR (length(title) BETWEEN 1 AND 255"
            " AND title !~ '^[[:space:]]|[[:space:]]$')",
            name="ck_upload_sessions_title",
        ),
        CheckConstraint(
            "length(content_type) BETWEEN 1 AND 255", name="ck_upload_sessions_content_type"
        ),
        CheckConstraint("size_bytes >= 1", name="ck_upload_sessions_size_bytes"),
        CheckConstraint(f"sha256 ~ '{SHA256_PATTERN}'", name="ck_upload_sessions_sha256"),
        CheckConstraint("length(object_key) > 0", name="ck_upload_sessions_object_key"),
        CheckConstraint("expires_at > created_at", name="ck_upload_sessions_expires_after_created"),
        CheckConstraint(
            "(state = 'failed') = (failure_code IS NOT NULL)",
            name="ck_upload_sessions_failure_code",
        ),
        CheckConstraint(
            "(state = 'finalized') = (finalized_at IS NOT NULL)",
            name="ck_upload_sessions_finalized_at",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_upload_sessions_workspace",
            ondelete="CASCADE",
        ),
        Index("ix_upload_sessions_org_id_state", "org_id", "state"),
        Index("ix_upload_sessions_org_id_created_by_state", "org_id", "created_by", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_upload_sessions_org_id", ondelete="CASCADE"),
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    # Reserved ids: the document may not exist yet (it is created at finalize), so no FK.
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_upload_sessions_created_by")
    )
    filename: Mapped[str] = mapped_column(Text)
    # Optional title for a NEW document, kept until finalize (NULL = derive it from the filename).
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(Text)
    object_key: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default=UPLOAD_STATE_PENDING)
    failure_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(_tstz())
    finalized_at: Mapped[datetime | None] = mapped_column(_tstz())


ORG_SCOPED_TABLES: Final = (
    "organizations",
    "organization_memberships",
    "workspaces",
    "workspace_memberships",
    "resource_grants",
    "audit_events",
    "sources",
    "documents",
    "document_versions",
    "upload_sessions",
    # M03 (migration 0005, docs/architecture/m03-contracts.md §3).
    "ingestion_jobs",
    "ingestion_job_events",
    "ingestion_checkpoints",
    "knowledge_revisions",
    "document_publications",
    "outbox_events",
    # M04 (migration 0007, docs/architecture/m04-contracts.md §5, §8).
    "ai_policies",
    "chat_threads",
    "chat_messages",
    "chat_runs",
    "chat_run_events",
    # M06 (migration 0009, docs/architecture/m06-contracts.md section 2).
    "connector_accounts",
    "source_syncs",
    "source_items",
    # M05 (migration 0008, docs/architecture/m05-contracts.md §3).
    "graph_entities",
    "entity_merge_candidates",
    # M07 (migration 0010, docs/architecture/m07-contracts.md section 2).
    "memories",
    "research_sessions",
    "research_steps",
    "research_reports",
    # Member pre-approvals (migration 0011).
    "org_member_preapprovals",
    # M11.01 usage metering (migration 0012).
    "usage_events",
    # Embedding cache (migration 0013, M11.03).
    "embedding_cache",
    # M12.01 (migration 0016): global defaults (org_id NULL) are readable by every org.
    "prompt_versions",
    # Workspace golden sets (migration 0017_evals, M12.03).
    "eval_sets",
    "eval_cases",
    "eval_runs",
    "eval_run_results",
)
RLS_TABLES: Final = ("users", *ORG_SCOPED_TABLES)

# Register the M03 models on ``Base.metadata`` (they live in the ingestion module; imported last
# because they import ``Base`` from here). Keeps ``Base.metadata.tables == RLS_TABLES`` true for
# every importer of this module.
from app.modules.ai import models_db as _ai_models  # noqa: E402, F401
from app.modules.ai.embeddings import models as _embedding_models  # noqa: E402, F401
from app.modules.connectors import models as _connector_models  # noqa: E402, F401
from app.modules.evals import models as _eval_models  # noqa: E402, F401
from app.modules.graph import models as _graph_models  # noqa: E402, F401
from app.modules.ingestion import models as _ingestion_models  # noqa: E402, F401
from app.modules.memory import models as _memory_models  # noqa: E402, F401
from app.modules.prompts import models as _prompt_models  # noqa: E402, F401
from app.modules.research import models as _research_models  # noqa: E402, F401
from app.modules.usage import models as _usage_models  # noqa: E402, F401
