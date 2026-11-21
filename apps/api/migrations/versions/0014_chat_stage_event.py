"""Chat run event type ``stage`` (M11.07): additive, lets the UI show "verifying".

Widens ``ck_chat_run_events_type``; existing rows and clients are unaffected.

Revision ID: 0014_chat_stage_event
Revises: 0013_embedding_cache
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_chat_stage_event"
down_revision: str | None = "0013_embedding_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = (
    "('started', 'retrieval', 'delta', 'citations', 'verification', 'completed', 'failed', "
    "'cancelled')"
)
NEW = (
    "('started', 'retrieval', 'delta', 'citations', 'verification', 'completed', 'failed', "
    "'cancelled', 'stage')"
)


def _set(types: str) -> None:
    op.execute("ALTER TABLE chat_run_events DROP CONSTRAINT ck_chat_run_events_type")
    op.execute(
        "ALTER TABLE chat_run_events ADD CONSTRAINT ck_chat_run_events_type "
        f"CHECK (type IN {types})"
    )


def upgrade() -> None:
    _set(NEW)


def downgrade() -> None:
    op.execute("DELETE FROM chat_run_events WHERE type = 'stage'")
    _set(OLD)
