"""M13.02 redacted prompt/response capture (O2).

* ``ai_policies`` gains ``capture_mode`` (``off`` default | ``redacted`` | ``full_admin_only``) and
  ``capture_retention_days`` (1-90, default 14).
* ``ai_run_captures``: one row per captured chat run (hard length limits, ids of the evidence used,
  ``expires_at``). Written by the worker role, read/deleted by the API (org owner/admin only,
  enforced in the API). FORCE RLS by org.
* ``ai_run_captures_sweep()``: SECURITY DEFINER, deletes expired rows across orgs (worker).

Revision ID: 0020_ai_run_captures
Revises: 0019c_shadow_runs
Create Date: 2026-11-22
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0020_ai_run_captures"
down_revision: str | None = "0019c_shadow_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SWEEP = "ai_run_captures_sweep()"


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
ALTER TABLE ai_policies
    ADD COLUMN capture_mode text NOT NULL DEFAULT 'off',
    ADD COLUMN capture_retention_days integer NOT NULL DEFAULT 14,
    ADD CONSTRAINT ck_ai_policies_capture_mode
        CHECK (capture_mode IN ('off', 'redacted', 'full_admin_only')),
    ADD CONSTRAINT ck_ai_policies_capture_retention
        CHECK (capture_retention_days BETWEEN 1 AND 90);

CREATE TABLE ai_run_captures (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    org_id uuid NOT NULL,
    workspace_id uuid,
    run_id uuid NOT NULL,
    kind text NOT NULL DEFAULT 'chat',
    mode text NOT NULL,
    prompt_version_id uuid,
    messages text NOT NULL,
    answer text NOT NULL,
    chunk_ids uuid[] NOT NULL DEFAULT '{}',
    document_ids uuid[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    CONSTRAINT pk_ai_run_captures PRIMARY KEY (id),
    CONSTRAINT uq_ai_run_captures_run_kind UNIQUE (run_id, kind),
    CONSTRAINT ck_ai_run_captures_kind CHECK (kind IN ('chat')),
    CONSTRAINT ck_ai_run_captures_mode CHECK (mode IN ('redacted', 'full_admin_only')),
    CONSTRAINT ck_ai_run_captures_messages CHECK (length(messages) <= 32000),
    CONSTRAINT ck_ai_run_captures_answer CHECK (length(answer) <= 16000),
    CONSTRAINT fk_ai_run_captures_org FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_ai_run_captures_run FOREIGN KEY (run_id) REFERENCES chat_runs (id)
);
CREATE INDEX ix_ai_run_captures_org_created ON ai_run_captures (org_id, created_at DESC);
CREATE INDEX ix_ai_run_captures_expires ON ai_run_captures (expires_at);
CREATE INDEX ix_ai_run_captures_documents ON ai_run_captures USING gin (document_ids);

CREATE FUNCTION ai_run_captures_sweep() RETURNS bigint
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    WITH d AS (DELETE FROM public.ai_run_captures WHERE expires_at <= now() RETURNING 1)
    SELECT count(*) FROM d
$$;
"""


def upgrade() -> None:
    app = _role("app_role", "POSTGRES_APP_USER", "ukos_app")
    worker = _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")
    op.execute(SCHEMA_SQL)
    op.execute("ALTER TABLE ai_run_captures ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ai_run_captures FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY ai_run_captures_org ON ai_run_captures USING (org_id = {CUR_ORG})"
        f" WITH CHECK (org_id = {CUR_ORG})"
    )
    op.execute(f'GRANT UPDATE (capture_mode, capture_retention_days) ON ai_policies TO "{app}"')
    op.execute(f'GRANT SELECT, DELETE ON ai_run_captures TO "{app}"')
    op.execute(f'GRANT SELECT, INSERT, DELETE ON ai_run_captures TO "{worker}"')
    op.execute(f"REVOKE ALL ON FUNCTION {SWEEP} FROM PUBLIC")
    op.execute(f'GRANT EXECUTE ON FUNCTION {SWEEP} TO "{worker}"')


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SWEEP}")
    op.execute("DROP TABLE IF EXISTS ai_run_captures")
    op.execute(
        "ALTER TABLE ai_policies DROP CONSTRAINT IF EXISTS ck_ai_policies_capture_retention,"
        " DROP CONSTRAINT IF EXISTS ck_ai_policies_capture_mode,"
        " DROP COLUMN IF EXISTS capture_retention_days, DROP COLUMN IF EXISTS capture_mode"
    )
