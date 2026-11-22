"""M12.09 prompt A/B shadow runs.

* ``shadow_candidates``: per ``(org, kind)`` the draft prompt version marked as shadow candidate and
  its sampling rate (0-20 %, 0 = off). Written by the app role (the API enforces org owner/admin).
* ``shadow_runs``: one comparison row per sampled real chat run. Ids, statuses and numbers ONLY:
  never a question, an answer or any evidence text. Written by the worker role, read by the API
  (org owner/admin only, enforced in the API). FORCE RLS by org.

Revision ID: 0019c_shadow_runs
Revises: 0019b_workspace_styles
Create Date: 2026-11-21
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0019c_shadow_runs"
down_revision: str | None = "0019b_workspace_styles"
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
CREATE TABLE shadow_candidates (
    org_id uuid NOT NULL,
    kind text NOT NULL,
    version_id uuid NOT NULL,
    rate_pct integer NOT NULL DEFAULT 0,
    updated_by uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_shadow_candidates PRIMARY KEY (org_id, kind),
    CONSTRAINT ck_shadow_candidates_kind CHECK (kind = 'chat_system'),
    CONSTRAINT ck_shadow_candidates_rate CHECK (rate_pct BETWEEN 0 AND 20),
    CONSTRAINT fk_shadow_candidates_org FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_shadow_candidates_version FOREIGN KEY (version_id)
        REFERENCES prompt_versions (id) ON DELETE CASCADE
);
CREATE TABLE shadow_runs (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    run_id uuid NOT NULL,
    candidate_version_id uuid NOT NULL,
    kind text NOT NULL DEFAULT 'chat_system',
    status text NOT NULL DEFAULT 'running',
    skip_reason text,
    live_status text,
    live_citation_validity double precision,
    live_latency_ms integer,
    cand_status text,
    cand_citation_validity double precision,
    cand_latency_ms integer,
    tokens_in integer,
    tokens_out integer,
    outcome text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CONSTRAINT pk_shadow_runs PRIMARY KEY (id),
    CONSTRAINT uq_shadow_runs_run_candidate UNIQUE (run_id, candidate_version_id),
    CONSTRAINT ck_shadow_runs_status
        CHECK (status IN ('running', 'completed', 'skipped', 'failed')),
    CONSTRAINT ck_shadow_runs_outcome CHECK (outcome IS NULL OR outcome IN ('win', 'tie', 'loss')),
    CONSTRAINT fk_shadow_runs_org FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_shadow_runs_run FOREIGN KEY (run_id) REFERENCES chat_runs (id) ON DELETE CASCADE,
    CONSTRAINT fk_shadow_runs_version FOREIGN KEY (candidate_version_id)
        REFERENCES prompt_versions (id) ON DELETE CASCADE
);
CREATE INDEX ix_shadow_runs_org_created ON shadow_runs (org_id, created_at DESC);
CREATE INDEX ix_shadow_runs_candidate ON shadow_runs (candidate_version_id);
"""


def upgrade() -> None:
    app = _role("app_role", "POSTGRES_APP_USER", "ukos_app")
    worker = _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")
    op.execute(SCHEMA_SQL)
    for table in ("shadow_candidates", "shadow_runs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_org ON {table} USING (org_id = {CUR_ORG})"
            f" WITH CHECK (org_id = {CUR_ORG})"
        )
    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON shadow_candidates TO "{app}"')
    op.execute(f'GRANT SELECT ON shadow_candidates TO "{worker}"')
    op.execute(f'GRANT SELECT ON shadow_runs TO "{app}"')
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON shadow_runs TO "{worker}"')


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS shadow_runs")
    op.execute("DROP TABLE IF EXISTS shadow_candidates")
