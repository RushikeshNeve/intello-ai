"""M14.01/03: per-org firewall policy (``firewall_policies``).

One row per org (no row = everything off): modes for the input guard (user message, retrieved
evidence), the output guard, and the input sensitivity threshold. Written by the app role (the API
enforces org owner/admin), read by the worker role. FORCE RLS by org.

Revision ID: 0023_firewall_policies
Revises: 0019c_shadow_runs
Create Date: 2026-11-22
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0023_firewall_policies"
down_revision: str | None = "0019c_shadow_runs"
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
CREATE TABLE firewall_policies (
    org_id uuid NOT NULL,
    input_user_mode text NOT NULL DEFAULT 'off',
    input_evidence_mode text NOT NULL DEFAULT 'off',
    output_mode text NOT NULL DEFAULT 'off',
    threshold double precision NOT NULL DEFAULT 0.5,
    updated_by uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_firewall_policies PRIMARY KEY (org_id),
    CONSTRAINT ck_firewall_policies_input_user CHECK (input_user_mode IN ('off', 'flag', 'block')),
    CONSTRAINT ck_firewall_policies_input_evidence
        CHECK (input_evidence_mode IN ('off', 'flag', 'block')),
    CONSTRAINT ck_firewall_policies_output
        CHECK (output_mode IN ('off', 'flag', 'block', 'redact')),
    CONSTRAINT ck_firewall_policies_threshold CHECK (threshold BETWEEN 0.05 AND 1.0),
    CONSTRAINT fk_firewall_policies_org FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE
);
"""


def upgrade() -> None:
    app = _role("app_role", "POSTGRES_APP_USER", "ukos_app")
    worker = _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")
    op.execute(SCHEMA_SQL)
    op.execute("ALTER TABLE firewall_policies ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE firewall_policies FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY firewall_policies_org ON firewall_policies USING (org_id = {CUR_ORG})"
        f" WITH CHECK (org_id = {CUR_ORG})"
    )
    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON firewall_policies TO "{app}"')
    op.execute(f'GRANT SELECT ON firewall_policies TO "{worker}"')


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS firewall_policies")
