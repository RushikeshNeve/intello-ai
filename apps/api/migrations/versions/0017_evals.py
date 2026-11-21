"""Workspace golden sets (M12.03, feature E1): eval sets, cases, runs and run results.

Contract: docs/architecture/m12-contracts.md section "M12.03".

* All four tables carry ``org_id`` and ``workspace_id`` (FORCE RLS). A row is visible and writable
  only to a workspace admin of that workspace (``memory_ws_access(workspace_id) = 'admin'``, which
  includes org owner/admin) inside the context org. Nothing here is readable by plain members.
* ``eval_sets``: SELECT / INSERT / UPDATE / DELETE (``version`` is bumped by the API on every case
  change). ``eval_cases``: SELECT / INSERT / UPDATE / DELETE. ``eval_runs`` and
  ``eval_run_results``: SELECT / INSERT only (a run is written once, finished; deleting a set
  cascades through the foreign keys, which are exempt from RLS).

Revision ID: 0017_evals
Revises: 0016_prompt_versions
Create Date: 2026-11-21
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0017_evals"
down_revision: str | None = "0016_prompt_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
CUR_USER = "nullif(current_setting('app.current_user_id', true), '')::uuid"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

TABLES = ("eval_sets", "eval_cases", "eval_runs", "eval_run_results")


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


SCHEMA_SQL = """
CREATE TABLE eval_sets (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    name text NOT NULL,
    version integer NOT NULL DEFAULT 1,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_eval_sets PRIMARY KEY (id),
    CONSTRAINT uq_eval_sets_scope UNIQUE (id, org_id, workspace_id),
    CONSTRAINT ck_eval_sets_name CHECK (length(name) BETWEEN 1 AND 200),
    CONSTRAINT ck_eval_sets_version CHECK (version >= 1),
    CONSTRAINT fk_eval_sets_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_eval_sets_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_eval_sets_created_by FOREIGN KEY (created_by) REFERENCES users (id)
);
CREATE INDEX ix_eval_sets_workspace_id ON eval_sets (workspace_id, created_at, id);

CREATE TABLE eval_cases (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    set_id uuid NOT NULL,
    question text NOT NULL,
    expected_document_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    expected_chunk_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    should_abstain boolean NOT NULL DEFAULT false,
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_eval_cases PRIMARY KEY (id),
    CONSTRAINT uq_eval_cases_scope UNIQUE (id, org_id, workspace_id),
    CONSTRAINT ck_eval_cases_question CHECK (length(question) BETWEEN 1 AND 1000),
    CONSTRAINT ck_eval_cases_expected_docs CHECK (jsonb_typeof(expected_document_ids) = 'array'),
    CONSTRAINT ck_eval_cases_expected_chunks CHECK (jsonb_typeof(expected_chunk_ids) = 'array'),
    CONSTRAINT fk_eval_cases_set FOREIGN KEY (set_id, org_id, workspace_id)
        REFERENCES eval_sets (id, org_id, workspace_id) ON DELETE CASCADE
);
CREATE INDEX ix_eval_cases_set_id ON eval_cases (set_id, created_at, id);

CREATE TABLE eval_runs (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    set_id uuid NOT NULL,
    set_version integer NOT NULL,
    state text NOT NULL DEFAULT 'completed',
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by uuid NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CONSTRAINT pk_eval_runs PRIMARY KEY (id),
    CONSTRAINT uq_eval_runs_scope UNIQUE (id, org_id, workspace_id),
    CONSTRAINT ck_eval_runs_state CHECK (state IN ('running', 'completed', 'failed')),
    CONSTRAINT fk_eval_runs_set FOREIGN KEY (set_id, org_id, workspace_id)
        REFERENCES eval_sets (id, org_id, workspace_id) ON DELETE CASCADE,
    CONSTRAINT fk_eval_runs_created_by FOREIGN KEY (created_by) REFERENCES users (id)
);
CREATE INDEX ix_eval_runs_set_id ON eval_runs (set_id, started_at, id);

CREATE TABLE eval_run_results (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    case_id uuid,
    question text NOT NULL,
    retrieved jsonb NOT NULL DEFAULT '[]'::jsonb,
    answer_status text NOT NULL,
    metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT pk_eval_run_results PRIMARY KEY (id),
    CONSTRAINT ck_eval_run_results_status CHECK (
        answer_status IN ('answered', 'abstained', 'error')),
    CONSTRAINT ck_eval_run_results_retrieved CHECK (jsonb_typeof(retrieved) = 'array'),
    CONSTRAINT fk_eval_run_results_run FOREIGN KEY (run_id, org_id, workspace_id)
        REFERENCES eval_runs (id, org_id, workspace_id) ON DELETE CASCADE
);
CREATE INDEX ix_eval_run_results_run_id ON eval_run_results (run_id);
"""

_ADMIN = "memory_ws_access(workspace_id) = 'admin'"


def _rls() -> str:
    parts: list[str] = []
    for table, extra_check in (
        ("eval_sets", f" AND created_by = {CUR_USER}"),
        ("eval_cases", ""),
        ("eval_runs", f" AND created_by = {CUR_USER}"),
        ("eval_run_results", ""),
    ):
        base = f"org_id = {CUR_ORG} AND {_ADMIN}"
        parts.append(f"CREATE POLICY {table}_select ON {table} FOR SELECT USING ({base});")
        parts.append(
            f"CREATE POLICY {table}_insert ON {table} FOR INSERT WITH CHECK ({base}{extra_check});"
        )
    for table in ("eval_sets", "eval_cases"):
        base = f"org_id = {CUR_ORG} AND {_ADMIN}"
        parts.append(
            f"CREATE POLICY {table}_update ON {table} FOR UPDATE USING ({base})"
            f" WITH CHECK ({base});"
        )
        parts.append(f"CREATE POLICY {table}_delete ON {table} FOR DELETE USING ({base});")
    return "\n".join(parts)


def upgrade() -> None:
    app = _app_role()
    op.execute(SCHEMA_SQL)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(_rls())
    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON eval_sets, eval_cases TO "{app}"')
    op.execute(f'GRANT SELECT, INSERT ON eval_runs, eval_run_results TO "{app}"')


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
