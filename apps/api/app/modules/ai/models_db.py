"""ORM models of the M04 chat and AI-policy schema (contract m04-contracts.md §5, §8).

The schema is owned by Alembic (``migrations/versions/0007_chat_and_ai_policy.py``); these models
mirror it, including constraint and index names. Row-level security (threads are private to their
creator), the append-only triggers on ``chat_messages``/``chat_run_events`` and the SECURITY DEFINER
functions exist only in the migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, _in, _tstz

__all__ = [
    "AGGREGATE_CHAT_RUN",
    "AI_PROVIDERS",
    "CHAT_TABLES",
    "EVENT_CHAT_RUN_QUEUED",
    "ROLE_ASSISTANT",
    "ROLE_USER",
    "AiPolicy",
    "ChatMessageRow",
    "ChatRun",
    "ChatRunEvent",
    "ChatThread",
]

PROVIDER_LOCAL: Final = "local"
PROVIDER_OPENAI: Final = "openai"
AI_PROVIDERS: Final = (PROVIDER_LOCAL, PROVIDER_OPENAI)

MESSAGE_ROLES: Final = ("user", "assistant")
ROLE_USER: Final = "user"
ROLE_ASSISTANT: Final = "assistant"

RUN_STATES: Final = ("queued", "running", "completed", "failed", "cancelled")
RUN_STATE_QUEUED: Final = "queued"
RUN_STATE_RUNNING: Final = "running"
RUN_STATE_COMPLETED: Final = "completed"
RUN_STATE_FAILED: Final = "failed"
RUN_STATE_CANCELLED: Final = "cancelled"
ACTIVE_RUN_STATES: Final = (RUN_STATE_QUEUED, RUN_STATE_RUNNING)
TERMINAL_RUN_STATES: Final = (RUN_STATE_COMPLETED, RUN_STATE_FAILED, RUN_STATE_CANCELLED)

EVENT_TYPES: Final = (
    "started",
    "retrieval",
    "delta",
    "citations",
    "verification",
    "completed",
    "failed",
    "cancelled",
    "stage",
)

AGGREGATE_CHAT_RUN: Final = "chat_run"
# Same literal as ingestion.models.EVENT_CHAT_RUN_QUEUED (not imported: it would be a cycle).
EVENT_CHAT_RUN_QUEUED: Final = "chat.run.queued"

TITLE_MAX_LENGTH: Final = 200
CONTENT_MAX_LENGTH: Final = 100_000
IDEMPOTENCY_KEY_MAX_LENGTH: Final = 128
ERROR_CODE_MAX_LENGTH: Final = 64
EVENT_DATA_MAX_BYTES: Final = 65_536

CHAT_TABLES: Final = (
    "ai_policies",
    "chat_threads",
    "chat_messages",
    "chat_runs",
    "chat_run_events",
)

# Constraint names callers may map to behaviour.
RUN_IDEMPOTENCY_UNIQUE: Final = "uq_chat_runs_thread_id_idempotency_key"
MESSAGES_APPEND_ONLY_TRIGGER: Final = "chat_messages_append_only"  # SQLSTATE 42501
EVENTS_APPEND_ONLY_TRIGGER: Final = "chat_run_events_append_only"  # SQLSTATE 42501


class AiPolicy(Base):
    """One row per org; no row = the default (local only, nothing external)."""

    __tablename__ = "ai_policies"
    __table_args__ = (
        CheckConstraint(
            "cardinality(allowed_providers) >= 1"
            " AND allowed_providers <@ ARRAY['local', 'openai']::text[]",
            name="ck_ai_policies_providers",
        ),
        CheckConstraint(
            "allow_external OR allowed_providers <@ ARRAY['local']::text[]",
            name="ck_ai_policies_external",
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_ai_policies_org_id", ondelete="CASCADE"),
        primary_key=True,
    )
    allow_external: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    allowed_providers: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("ARRAY['local']::text[]")
    )
    # M13.02: prompt/response capture (off by default; retention 1-90 days)
    capture_mode: Mapped[str] = mapped_column(Text, server_default=text("'off'"))
    capture_retention_days: Mapped[int] = mapped_column(Integer, server_default=text("14"))
    pii_outbound: Mapped[str] = mapped_column(Text, server_default=text("'off'"))  # M14.04
    pii_restore: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    pii_secrets: Mapped[str] = mapped_column(Text, server_default=text("'block'"))
    hosted_models_allowed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_ai_policies_updated_by")
    )
    updated_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))


class ChatThread(Base):
    """Private to ``created_by`` (RLS). ``workspace_id`` NULL = org-wide search scope."""

    __tablename__ = "chat_threads"
    __table_args__ = (
        UniqueConstraint("id", "org_id", name="uq_chat_threads_id_org_id"),
        CheckConstraint(f"length(title) <= {TITLE_MAX_LENGTH}", name="ck_chat_threads_title"),
        ForeignKeyConstraint(
            ["workspace_id", "org_id"],
            ["workspaces.id", "workspaces.org_id"],
            name="fk_chat_threads_workspace",
            ondelete="CASCADE",
        ),
        Index(
            "ix_chat_threads_org_id_created_by_created_at",
            "org_id",
            "created_by",
            "created_at",
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_chat_threads_org_id", ondelete="CASCADE"),
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_chat_threads_created_by")
    )
    title: Mapped[str] = mapped_column(Text, server_default="")
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    deleted_at: Mapped[datetime | None] = mapped_column(_tstz())


class ChatRun(Base):
    __tablename__ = "chat_runs"
    __table_args__ = (
        UniqueConstraint("thread_id", "idempotency_key", name=RUN_IDEMPOTENCY_UNIQUE),
        UniqueConstraint("id", "org_id", name="uq_chat_runs_id_org_id"),
        UniqueConstraint("id", "thread_id", name="uq_chat_runs_id_thread_id"),
        CheckConstraint(_in("state", RUN_STATES), name="ck_chat_runs_state"),
        CheckConstraint(
            f"length(idempotency_key) BETWEEN 1 AND {IDEMPOTENCY_KEY_MAX_LENGTH}",
            name="ck_chat_runs_idempotency_key",
        ),
        CheckConstraint("attempts >= 0", name="ck_chat_runs_attempts"),
        CheckConstraint("lease_epoch >= 0", name="ck_chat_runs_lease_epoch"),
        CheckConstraint(
            "(state = 'running') = (lease_owner IS NOT NULL)", name="ck_chat_runs_lease"
        ),
        CheckConstraint(
            "(state IN ('completed', 'failed', 'cancelled')) = (finished_at IS NOT NULL)",
            name="ck_chat_runs_finished",
        ),
        CheckConstraint(
            "error_code IS NULL OR (state IN ('failed', 'cancelled')"
            f" AND length(error_code) <= {ERROR_CODE_MAX_LENGTH})",
            name="ck_chat_runs_error_code",
        ),
        ForeignKeyConstraint(
            ["thread_id", "org_id"],
            ["chat_threads.id", "chat_threads.org_id"],
            name="fk_chat_runs_thread",
        ),
        # The run is inserted before its user message (the idempotency key decides the winner).
        ForeignKeyConstraint(
            ["user_message_id", "thread_id"],
            ["chat_messages.id", "chat_messages.thread_id"],
            name="fk_chat_runs_user_message",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        Index("ix_chat_runs_thread_id_created_at", "thread_id", "created_at"),
        Index(
            "ix_chat_runs_active_created_by",
            "org_id",
            "created_by",
            postgresql_where=text("state IN ('queued', 'running')"),
        ),
        Index(
            "ix_chat_runs_running_lease",
            "lease_expires_at",
            postgresql_where=text("state = 'running'"),
        ),
        Index("ix_chat_runs_created_by_created_at", "created_by", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_chat_runs_org_id", ondelete="CASCADE"),
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    user_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", name="fk_chat_runs_created_by")
    )
    state: Mapped[str] = mapped_column(Text, server_default=RUN_STATE_QUEUED)
    idempotency_key: Mapped[str] = mapped_column(Text)
    scope_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    model: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error_code: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
    started_at: Mapped[datetime | None] = mapped_column(_tstz())
    finished_at: Mapped[datetime | None] = mapped_column(_tstz())
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(_tstz())
    lease_epoch: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))


class ChatMessageRow(Base):
    """Append-only (trigger). Named ``...Row`` to stay clear of the gateway's ``ChatMessage``."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("id", "thread_id", name="uq_chat_messages_id_thread_id"),
        CheckConstraint(_in("role", MESSAGE_ROLES), name="ck_chat_messages_role"),
        CheckConstraint(
            f"length(content) BETWEEN 1 AND {CONTENT_MAX_LENGTH}", name="ck_chat_messages_content"
        ),
        CheckConstraint("jsonb_typeof(citations) = 'array'", name="ck_chat_messages_citations"),
        ForeignKeyConstraint(
            ["thread_id", "org_id"],
            ["chat_threads.id", "chat_threads.org_id"],
            name="fk_chat_messages_thread",
        ),
        ForeignKeyConstraint(
            ["run_id", "thread_id"],
            ["chat_runs.id", "chat_runs.thread_id"],
            name="fk_chat_messages_run",
        ),
        Index("ix_chat_messages_thread_id_created_at", "thread_id", "created_at", "id"),
        Index(
            "ix_chat_messages_run_id",
            "run_id",
            postgresql_where=text("run_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_chat_messages_org_id", ondelete="CASCADE"),
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))


class ChatRunEvent(Base):
    """Append-only (trigger): the persisted stream of a run; clients replay it by ``seq``."""

    __tablename__ = "chat_run_events"
    __table_args__ = (
        CheckConstraint(_in("type", EVENT_TYPES), name="ck_chat_run_events_type"),
        CheckConstraint(
            f"octet_length(data::text) <= {EVENT_DATA_MAX_BYTES}", name="ck_chat_run_events_data"
        ),
        ForeignKeyConstraint(
            ["run_id", "org_id"],
            ["chat_runs.id", "chat_runs.org_id"],
            name="fk_chat_run_events_run",
        ),
        Index("ix_chat_run_events_run_id_seq", "run_id", "seq"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", name="fk_chat_run_events_org_id", ondelete="CASCADE"),
    )
    type: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(_tstz(), server_default=text("now()"))
