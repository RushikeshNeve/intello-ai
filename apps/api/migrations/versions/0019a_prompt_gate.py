"""Prompt change gate (M12.06): evidence columns on ``prompt_versions`` and per-org gate settings.

* ``prompt_versions.eval_run_id`` / ``eval_attestation``: nullable evidence reference stored when a
  version is activated (a workspace golden-set run id, or a CI-signed offline attestation id).
* ``prompt_gate_settings``: one row per org (``enforced`` default false, thresholds). RLS by org.

Revision ID: 0019a_prompt_gate
Revises: 0015b_cache_evict_orgs
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0019a_prompt_gate"
down_revision: str | None = "0015b_cache_evict_orgs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _app_role() -> str:
    value = (
        context.config.attributes.get("app_role")
        or context.get_x_argument(as_dictionary=True).get("app_role")
        or os.environ.get("POSTGRES_APP_USER")
        or "ukos_app"
    )
    if not ROLE_PATTERN.fullmatch(value):
        raise ValueError("invalid app_role name")
    return str(value)


def upgrade() -> None:
    app = _app_role()
    op.execute(
        "ALTER TABLE prompt_versions ADD COLUMN eval_run_id uuid, ADD COLUMN eval_attestation text"
    )
    op.execute(
        f"""
        CREATE TABLE prompt_gate_settings (
            org_id uuid NOT NULL,
            enforced boolean NOT NULL DEFAULT false,
            min_pass_rate numeric(4,3) NOT NULL DEFAULT 0.800,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_prompt_gate_settings PRIMARY KEY (org_id),
            CONSTRAINT ck_prompt_gate_min_pass CHECK (min_pass_rate BETWEEN 0 AND 1),
            CONSTRAINT fk_prompt_gate_settings_org FOREIGN KEY (org_id)
                REFERENCES organizations (id) ON DELETE CASCADE
        );
        ALTER TABLE prompt_gate_settings ENABLE ROW LEVEL SECURITY;
        ALTER TABLE prompt_gate_settings FORCE ROW LEVEL SECURITY;
        CREATE POLICY prompt_gate_settings_all ON prompt_gate_settings
            USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});
        """
    )
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON prompt_gate_settings TO "{app}"')
    op.execute(f'GRANT UPDATE (eval_run_id, eval_attestation) ON prompt_versions TO "{app}"')


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS prompt_gate_settings")
    op.execute(
        "ALTER TABLE prompt_versions DROP COLUMN IF EXISTS eval_run_id,"
        " DROP COLUMN IF EXISTS eval_attestation"
    )
