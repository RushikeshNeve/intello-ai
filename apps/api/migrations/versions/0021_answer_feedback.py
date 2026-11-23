"""M13.03 answer feedback (O5): thumbs + reason code per assistant message, no free text.

``answer_feedback``: one row per (message, user). FORCE RLS: a user reads/writes their own rows;
org owners/admins read all rows of the org (for the golden-set review export).

Revision ID: 0021_answer_feedback
Revises: 0020_ai_run_captures
Create Date: 2026-11-22
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0021_answer_feedback"
down_revision: str | None = "0020_ai_run_captures"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
CUR_USER = "nullif(current_setting('app.current_user_id', true), '')::uuid"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _role(name: str, env_var: str, default: str) -> str:
    value = (
        context.config.attributes.get(name)
        or context.get_x_argument(as_dictionary=True).get(name)
        or os.environ.get(env_var)
        or default
    )
    if not ROLE_PATTERN.fullmatch(value):
        raise ValueError(f"invalid {name} name")
    return str(value)


SCHEMA_SQL = """
CREATE TABLE answer_feedback (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    org_id uuid NOT NULL,
    workspace_id uuid,
    run_id uuid NOT NULL,
    message_id uuid NOT NULL,
    user_id uuid NOT NULL,
    rating text NOT NULL,
    reason text,
    chunk_ids uuid[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_answer_feedback PRIMARY KEY (id),
    CONSTRAINT uq_answer_feedback_message_user UNIQUE (message_id, user_id),
    CONSTRAINT ck_answer_feedback_rating CHECK (rating IN ('up', 'down')),
    CONSTRAINT ck_answer_feedback_reason CHECK (
        reason IS NULL OR (rating = 'down' AND reason IN
            ('wrong_answer', 'wrong_citation', 'missing_info', 'unsafe', 'other'))
    ),
    CONSTRAINT fk_answer_feedback_org FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_answer_feedback_message FOREIGN KEY (message_id)
        REFERENCES chat_messages (id),
    CONSTRAINT fk_answer_feedback_run FOREIGN KEY (run_id) REFERENCES chat_runs (id),
    CONSTRAINT fk_answer_feedback_user FOREIGN KEY (user_id) REFERENCES users (id)
);
CREATE INDEX ix_answer_feedback_org_rating ON answer_feedback (org_id, rating, created_at DESC);
"""

_IS_ADMIN = (
    "EXISTS (SELECT 1 FROM organization_memberships m WHERE m.org_id = answer_feedback.org_id"
    f" AND m.user_id = {CUR_USER} AND m.role IN ('owner', 'admin'))"
)


def upgrade() -> None:
    app = _role("app_role", "POSTGRES_APP_USER", "ukos_app")
    op.execute(SCHEMA_SQL)
    op.execute("ALTER TABLE answer_feedback ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE answer_feedback FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY answer_feedback_select ON answer_feedback FOR SELECT"
        f" USING (org_id = {CUR_ORG} AND (user_id = {CUR_USER} OR {_IS_ADMIN}))"
    )
    op.execute(
        "CREATE POLICY answer_feedback_insert ON answer_feedback FOR INSERT"
        f" WITH CHECK (org_id = {CUR_ORG} AND user_id = {CUR_USER})"
    )
    op.execute(
        "CREATE POLICY answer_feedback_update ON answer_feedback FOR UPDATE"
        f" USING (org_id = {CUR_ORG} AND user_id = {CUR_USER})"
        f" WITH CHECK (org_id = {CUR_ORG} AND user_id = {CUR_USER})"
    )
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON answer_feedback TO "{app}"')


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS answer_feedback")
