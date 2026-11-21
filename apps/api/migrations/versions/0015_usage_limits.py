"""Usage budgets and quotas (M11.08): ``usage_limits``.

One row = one limit: ``scope`` org (whole-org total) or user (``user_id`` null = applies to every
user separately, set = that user only), ``kind`` chat|embed|research|graph|all, ``period``
day|month (UTC), ``max_tokens`` and/or ``max_requests``, ``action`` warn|block. FORCE RLS by org.
The API role manages rows (admin checks in the service); the worker role only reads them.

Revision ID: 0015_usage_limits
Revises: 0018_reindex
Create Date: 2026-11-21
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0015_usage_limits"
down_revision: str | None = "0018_reindex"
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
CREATE TABLE usage_limits (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    scope text NOT NULL,
    user_id uuid,
    kind text NOT NULL,
    period text NOT NULL,
    max_tokens bigint,
    max_requests integer,
    action text NOT NULL,
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_usage_limits PRIMARY KEY (id),
    CONSTRAINT ck_usage_limits_scope CHECK (scope IN ('org', 'user')),
    CONSTRAINT ck_usage_limits_org_no_user CHECK (scope = 'user' OR user_id IS NULL),
    CONSTRAINT ck_usage_limits_kind CHECK (kind IN ('chat', 'embed', 'research', 'graph', 'all')),
    CONSTRAINT ck_usage_limits_period CHECK (period IN ('day', 'month')),
    CONSTRAINT ck_usage_limits_action CHECK (action IN ('warn', 'block')),
    CONSTRAINT ck_usage_limits_max CHECK (
        (max_tokens IS NOT NULL OR max_requests IS NOT NULL)
        AND coalesce(max_tokens, 0) >= 0 AND coalesce(max_requests, 0) >= 0),
    CONSTRAINT fk_usage_limits_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE
);
CREATE INDEX ix_usage_limits_org ON usage_limits (org_id);
CREATE INDEX ix_usage_events_org_user_created ON usage_events (org_id, user_id, created_at);
"""

RLS_SQL = f"""
CREATE POLICY usage_limits_select ON usage_limits FOR SELECT USING (org_id = {CUR_ORG});
CREATE POLICY usage_limits_insert ON usage_limits FOR INSERT WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY usage_limits_update ON usage_limits FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY usage_limits_delete ON usage_limits FOR DELETE USING (org_id = {CUR_ORG});
"""


def upgrade() -> None:
    app = _role("app_role", "POSTGRES_APP_USER", "ukos_app")
    worker = _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")
    op.execute(SCHEMA_SQL)
    op.execute("ALTER TABLE usage_limits ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE usage_limits FORCE ROW LEVEL SECURITY")
    op.execute(RLS_SQL)
    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON usage_limits TO "{app}"')
    op.execute(f'GRANT SELECT ON usage_limits TO "{worker}"')


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_usage_events_org_user_created")
    op.execute("DROP TABLE IF EXISTS usage_limits")
