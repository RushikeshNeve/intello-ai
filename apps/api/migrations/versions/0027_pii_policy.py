"""M14.04 PII policy (additive).

* ``ai_policies``: ``pii_outbound`` (off|redact|block, default off), ``pii_restore`` (default
  false), ``pii_secrets`` (block|redact, default block). Existing RLS and grants cover the columns.
* ``workspace_pii_settings``: per-workspace ingestion flags (record per-document PII counts by type,
  mask PII in indexed text); both default off. No PII value is ever stored. FORCE RLS by org.

Revision ID: 0027_pii_policy
Revises: 0026_tenant_keys
Create Date: 2026-11-22
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0027_pii_policy"
down_revision: str | None = "0026_tenant_keys"
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


COLUMNS_SQL = """
ALTER TABLE ai_policies
    ADD COLUMN pii_outbound text NOT NULL DEFAULT 'off',
    ADD COLUMN pii_restore boolean NOT NULL DEFAULT false,
    ADD COLUMN pii_secrets text NOT NULL DEFAULT 'block',
    ADD CONSTRAINT ck_ai_policies_pii_outbound CHECK (pii_outbound IN ('off', 'redact', 'block')),
    ADD CONSTRAINT ck_ai_policies_pii_secrets CHECK (pii_secrets IN ('block', 'redact'));
"""

SCHEMA_SQL = """
CREATE TABLE workspace_pii_settings (
    workspace_id uuid NOT NULL,
    org_id uuid NOT NULL,
    record_counts boolean NOT NULL DEFAULT false,
    mask_indexed boolean NOT NULL DEFAULT false,
    updated_by uuid NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_workspace_pii_settings PRIMARY KEY (workspace_id),
    CONSTRAINT fk_workspace_pii_settings_org FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_workspace_pii_settings_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_workspace_pii_settings_updated_by FOREIGN KEY (updated_by) REFERENCES users (id)
);
"""

_READ = f"org_id = {CUR_ORG} AND memory_ws_access(workspace_id) IS NOT NULL"
_WRITE = f"org_id = {CUR_ORG} AND memory_ws_access(workspace_id) = 'admin'"
RLS_SQL = f"""
CREATE POLICY workspace_pii_settings_select ON workspace_pii_settings FOR SELECT USING ({_READ});
CREATE POLICY workspace_pii_settings_insert ON workspace_pii_settings FOR INSERT
    WITH CHECK ({_WRITE} AND updated_by = {CUR_USER});
CREATE POLICY workspace_pii_settings_update ON workspace_pii_settings FOR UPDATE
    USING ({_WRITE}) WITH CHECK ({_WRITE} AND updated_by = {CUR_USER});
"""


def upgrade() -> None:
    app = _role("app_role", "POSTGRES_APP_USER", "ukos_app")
    worker = _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")
    op.execute(COLUMNS_SQL)
    op.execute(SCHEMA_SQL)
    op.execute("ALTER TABLE workspace_pii_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workspace_pii_settings FORCE ROW LEVEL SECURITY")
    op.execute(RLS_SQL)
    # The ingestion worker has an org context but no user: it reads the flags by org (M14.02 scan).
    op.execute(
        "CREATE POLICY workspace_pii_settings_worker ON workspace_pii_settings FOR SELECT"
        f' TO "{worker}" USING (org_id = {CUR_ORG})'
    )
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON workspace_pii_settings TO "{app}"')
    op.execute(f'GRANT SELECT ON workspace_pii_settings TO "{worker}"')


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workspace_pii_settings")
    op.execute(
        "ALTER TABLE ai_policies DROP CONSTRAINT IF EXISTS ck_ai_policies_pii_secrets,"
        " DROP CONSTRAINT IF EXISTS ck_ai_policies_pii_outbound,"
        " DROP COLUMN IF EXISTS pii_secrets, DROP COLUMN IF EXISTS pii_restore,"
        " DROP COLUMN IF EXISTS pii_outbound"
    )
