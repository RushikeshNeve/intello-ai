"""ORM models of the M03 ingestion schema (contract docs/architecture/m03-contracts.md §3).

The schema is owned by Alembic (``migrations/versions/0005_ingestion.py``); these models mirror it,
including constraint and index names, so an Alembic autogenerate comparison against a migrated
database finds no difference. Row-level security, the append-only trigger on
``ingestion_job_events``, the ``ukos_worker`` role and the SECURITY DEFINER functions exist only in
the migration (they are not expressible as ORM metadata).
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
    Identity,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import SHA256_PATTERN, Base, _in, _tstz

JOB_STATES: Final = ("queued", "running", "succeeded", "dead", "unsupported", "cancelled")
JOB_STATE_QUEUED: Final = "queued"
JOB_STATE_RUNNING: Final = "running"
JOB_STATE_SUCCEEDED: Final = "succeeded"
JOB_STATE_DEAD: Final = "dead"
JOB_STATE_UNSUPPORTED: Final = "unsupported"
JOB_STATE_CANCELLED: Final = "cancelled"

JOB_EVENT_TYPES: Final = (
    "queued",
    "started",
    "stage_started",
    "stage_completed",
    "retry_scheduled",
    "succeeded",
    "dead",
    "unsupported",
    "cancelled",
    "published",
)
STAGES: Final = ("extract", "chunk", "embed", "index", "validate", "publish")

CHECKPOINT_STATES: Final = ("running", "completed", "failed")

REVISION_STATES: Final = ("building", "ready", "published", "superseded", "failed", "purged")

EVENT_JOB_QUEUED: Final = "ingestion.job.queued"
EVENT_REVISION_PUBLISHED: Final = "knowledge.revision.published"
EVENT_DOCUMENT_DELETED: Final = "document.deleted"
EVENT_RECONCILE: Final = "knowledge.reconcile"
EVENT_CHAT_RUN_QUEUED: Final = "chat.run.queued"  # M04 (migration 0007)
OUTBOX_EVENT_TYPES: Final = (
    EVENT_JOB_QUEUED,
    EVENT_REVISION_PUBLISHED,
    EVENT_DOCUMENT_DELETED,
    EVENT_RECONCILE,
    EVENT_CHAT_RUN_QUEUED,
)

AGGREGATE_INGESTION_JOB: Final = "ingestion_job"
AGGREGATE_DOCUMENT: Final = "document"

ERROR_DETAIL_MAX_LENGTH: Final = 500

# Constraint names callers may map to behaviour.
JOB_REVISION_UNIQUE: Final = "uq_ingestion_jobs_revision_id"
JOB_EVENTS_APPEND_ONLY_TRIGGER: Final = "ingestion_job_events_append_only"  # SQLSTATE 42501

INGESTION_TABLES: Final = (
    "ingestion_jobs",
    "ingestion_job_events",
    "ingestion_checkpoints",
    "knowledge_revisions",
    "document_publications",
    "outbox_events",
)


def _fk_org(table: str) -> ForeignKey:
    return ForeignKey("organizations.id", name=f"fk_{table}_org_id", ondelete="CASCADE")


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        UniqueConstraint("revision_id", name=JOB_REVISION_UNIQUE),
        UniqueConstraint("id", "org_id", name="uq_ingestion_jobs_id_org_id"),
        CheckConstraint(_in("state", JOB_STATES), name="ck_ingestion_jobs_state"),
        CheckConstraint("attempts >= 0", name="ck_ingestion_jobs_attempts"),
        CheckConstraint("max_attempts >= 1", name="ck_ingestion_jobs_max_attempts"),
        CheckConstraint("lease_epoch >= 0", name="ck_ingestion_jobs_lease_epoch"),
        CheckConstraint(
            "(state = 'running') = (lease_owner IS NOT NULL)", name="ck_ingestion_jobs_lease"
        ),
        CheckConstraint(
            "current_stage IS NULL OR " + _in("current_stage", STAGES),
            name="ck_ingestion_jobs_current_stage",
        ),
        CheckConstraint(
            f"last_error_detail IS NULL OR length(last_error_detail) <= {ERROR_DETAIL_MAX_LENGTH}",
            name="ck_ingestion_jobs_last_error_detail",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_ingestion_jobs_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_ingestion_jobs_document",
        ),
        ForeignKeyConstraint(
            ["version_id", "document_id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_ingestion_jobs_version",
        ),
        Index("ix_ingestion_jobs_state_run_after", "state", "run_after"),
        Index("ix_ingestion_jobs_org_id_document_id", "org_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _fk_org("ingestion_jobs"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    pipeline_version: Mapped[str] = mapped_column(Text)
    embedding_space_id: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default=JOB_STATE_QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("5"))
    run_after: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(_tstz())
    lease_epoch: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    heartbeat_at: Mapped[datetime | None] = mapped_column(_tstz())
    current_stage: Mapped[str | None] = mapped_column(Text)
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    started_at: Mapped[datetime | None] = mapped_column(_tstz())
    finished_at: Mapped[datetime | None] = mapped_column(_tstz())


class IngestionJobEvent(Base):
    """Append-only (trigger): UPDATE, DELETE and TRUNCATE are rejected for every role."""

    __tablename__ = "ingestion_job_events"
    __table_args__ = (
        CheckConstraint(_in("event_type", JOB_EVENT_TYPES), name="ck_ingestion_job_events_type"),
        CheckConstraint(
            "stage IS NULL OR " + _in("stage", STAGES), name="ck_ingestion_job_events_stage"
        ),
        CheckConstraint("attempt IS NULL OR attempt >= 0", name="ck_ingestion_job_events_attempt"),
        ForeignKeyConstraint(
            ["job_id", "org_id"],
            ["ingestion_jobs.id", "ingestion_jobs.org_id"],
            name="fk_ingestion_job_events_job",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_ingestion_job_events_workspace",
        ),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_ingestion_job_events_document",
        ),
        ForeignKeyConstraint(
            ["version_id", "document_id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_ingestion_job_events_version",
        ),
        Index("ix_ingestion_job_events_job_id", "job_id"),
        Index("ix_ingestion_job_events_org_id_document_id", "org_id", "document_id", "seq"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _fk_org("ingestion_job_events"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(Text)
    stage: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))


class KnowledgeRevision(Base):
    __tablename__ = "knowledge_revisions"
    __table_args__ = (
        UniqueConstraint(
            "version_id",
            "pipeline_version",
            "embedding_space_id",
            name="uq_knowledge_revisions_version_pipeline_space",
        ),
        UniqueConstraint("id", "org_id", name="uq_knowledge_revisions_id_org_id"),
        UniqueConstraint("id", "document_id", name="uq_knowledge_revisions_id_document_id"),
        CheckConstraint(_in("state", REVISION_STATES), name="ck_knowledge_revisions_state"),
        CheckConstraint("chunk_count >= 0", name="ck_knowledge_revisions_chunk_count"),
        CheckConstraint(
            f"content_fingerprint IS NULL OR content_fingerprint ~ '{SHA256_PATTERN}'",
            name="ck_knowledge_revisions_content_fingerprint",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_knowledge_revisions_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_knowledge_revisions_document",
        ),
        ForeignKeyConstraint(
            ["version_id", "document_id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_knowledge_revisions_version",
        ),
        Index("ix_knowledge_revisions_org_id_document_id", "org_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _fk_org("knowledge_revisions"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    pipeline_version: Mapped[str] = mapped_column(Text)
    embedding_space_id: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="building")
    chunk_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    content_fingerprint: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    ready_at: Mapped[datetime | None] = mapped_column(_tstz())
    published_at: Mapped[datetime | None] = mapped_column(_tstz())
    superseded_at: Mapped[datetime | None] = mapped_column(_tstz())
    purged_at: Mapped[datetime | None] = mapped_column(_tstz())


class IngestionCheckpoint(Base):
    """Keyed by revision so a re-created job reuses finished work. ``org_id`` is carried for RLS."""

    __tablename__ = "ingestion_checkpoints"
    __table_args__ = (
        PrimaryKeyConstraint("revision_id", "stage", name="pk_ingestion_checkpoints"),
        CheckConstraint(_in("stage", STAGES), name="ck_ingestion_checkpoints_stage"),
        CheckConstraint(_in("state", CHECKPOINT_STATES), name="ck_ingestion_checkpoints_state"),
        CheckConstraint("attempts >= 0", name="ck_ingestion_checkpoints_attempts"),
        CheckConstraint(
            "artifact_bytes IS NULL OR artifact_bytes >= 0",
            name="ck_ingestion_checkpoints_artifact_bytes",
        ),
        CheckConstraint(
            f"artifact_sha256 IS NULL OR artifact_sha256 ~ '{SHA256_PATTERN}'",
            name="ck_ingestion_checkpoints_artifact_sha256",
        ),
        CheckConstraint(
            "(state = 'completed') = (completed_at IS NOT NULL)",
            name="ck_ingestion_checkpoints_completed_at",
        ),
        ForeignKeyConstraint(
            ["revision_id", "org_id"],
            ["knowledge_revisions.id", "knowledge_revisions.org_id"],
            name="fk_ingestion_checkpoints_revision",
            ondelete="CASCADE",
        ),
        Index("ix_ingestion_checkpoints_org_id", "org_id"),
    )

    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    stage: Mapped[str] = mapped_column(Text)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _fk_org("ingestion_checkpoints"))
    state: Mapped[str] = mapped_column(Text, server_default="running")
    input_fingerprint: Mapped[str] = mapped_column(Text)
    artifact_key: Mapped[str | None] = mapped_column(Text)
    artifact_sha256: Mapped[str | None] = mapped_column(Text)
    artifact_bytes: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    started_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(_tstz())
    error_code: Mapped[str | None] = mapped_column(Text)


class DocumentPublication(Base):
    """The publication pointer: at most one published revision per document (ADR-015)."""

    __tablename__ = "document_publications"
    __table_args__ = (
        CheckConstraint("version_number >= 1", name="ck_document_publications_version_number"),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_document_publications_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_document_publications_document",
        ),
        ForeignKeyConstraint(
            ["version_id", "document_id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_document_publications_version",
        ),
        ForeignKeyConstraint(
            ["revision_id", "document_id"],
            ["knowledge_revisions.id", "knowledge_revisions.document_id"],
            name="fk_document_publications_revision",
        ),
        ForeignKeyConstraint(
            ["previous_revision_id", "document_id"],
            ["knowledge_revisions.id", "knowledge_revisions.document_id"],
            name="fk_document_publications_previous_revision",
        ),
        Index("ix_document_publications_reconciled_at", "reconciled_at"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _fk_org("document_publications"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    version_number: Mapped[int] = mapped_column(Integer)
    previous_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    published_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    reconciled_at: Mapped[datetime | None] = mapped_column(_tstz())


class OutboxEvent(Base):
    """Transactional outbox: written in the business transaction, dispatched afterwards."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("seq", name="uq_outbox_events_seq"),
        CheckConstraint(_in("event_type", OUTBOX_EVENT_TYPES), name="ck_outbox_events_type"),
        CheckConstraint("length(aggregate_type) > 0", name="ck_outbox_events_aggregate_type"),
        CheckConstraint("attempts >= 0", name="ck_outbox_events_attempts"),
        CheckConstraint(
            f"last_error IS NULL OR length(last_error) <= {ERROR_DETAIL_MAX_LENGTH}",
            name="ck_outbox_events_last_error",
        ),
        Index(
            "ix_outbox_events_undispatched",
            "available_at",
            "seq",
            postgresql_where=text("dispatched_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), _fk_org("outbox_events"))
    aggregate_type: Mapped[str] = mapped_column(Text)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    available_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    dispatched_at: Mapped[datetime | None] = mapped_column(_tstz())
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
