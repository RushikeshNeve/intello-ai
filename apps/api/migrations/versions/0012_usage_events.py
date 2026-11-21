"""Usage metering (M11.01): one ``usage_events`` row per model call (chat, embed, research, graph).

FORCE RLS by org. The API role and the worker role may SELECT and INSERT rows of the context org
only; rows are immutable (no UPDATE, no DELETE) and go away with the org (ON DELETE CASCADE).
Rows carry counts, latency and an estimated cost only: never prompt or answer text.

Revision ID: 0012_usage_events
Revises: 0011_member_preapprovals
Create Date: 2026-11-21
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0012_usage_events"
down_revision: str | None = "0011_member_preapprovals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
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
CREATE TABLE usage_events (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid,
    user_id uuid,
    kind text NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    tokens_in integer NOT NULL DEFAULT 0,
    tokens_out integer NOT NULL DEFAULT 0,
    tokens_cached integer NOT NULL DEFAULT 0,
    latency_ms integer NOT NULL DEFAULT 0,
    est_cost_micros bigint NOT NULL DEFAULT 0,
    run_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_usage_events PRIMARY KEY (id),
    CONSTRAINT ck_usage_events_kind CHECK (kind IN ('chat', 'embed', 'research', 'graph')),
    CONSTRAINT ck_usage_events_nonneg CHECK (
        tokens_in >= 0 AND tokens_out >= 0 AND tokens_cached >= 0
        AND latency_ms >= 0 AND est_cost_micros >= 0),
    CONSTRAINT fk_usage_events_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE
);
CREATE INDEX ix_usage_events_org_created ON usage_events (org_id, created_at);
"""

RLS_SQL = f"""
CREATE POLICY usage_events_select ON usage_events FOR SELECT USING (org_id = {CUR_ORG});
CREATE POLICY usage_events_insert ON usage_events FOR INSERT WITH CHECK (org_id = {CUR_ORG});
"""


def upgrade() -> None:
    app = _role("app_role", "POSTGRES_APP_USER", "ukos_app")
    worker = _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")
    op.execute(SCHEMA_SQL)
    op.execute("ALTER TABLE usage_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE usage_events FORCE ROW LEVEL SECURITY")
    op.execute(RLS_SQL)
    op.execute(f'GRANT SELECT, INSERT ON usage_events TO "{app}", "{worker}"')


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS usage_events")
