"""M12.07 per-workspace answer styles (docs/architecture/m12-contracts.md, section M12.07).

``workspace_styles``: one row per (org, workspace) plus one org default (``workspace_id`` NULL).
FORCE RLS: a row is readable by anyone who may read its workspace (org default: any org-context
caller); a workspace row is writable by a workspace admin (incl. org owner/admin), the org default
by the API after its ``org.update`` check. The worker role only reads.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0019b_workspace_styles"
down_revision: str | None = "0018_reindex"
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
CREATE TABLE workspace_styles (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid,
    tone text NOT NULL DEFAULT 'neutral',
    answer_format text NOT NULL DEFAULT 'concise',
    language text,
    citation_style text NOT NULL DEFAULT 'numeric',
    custom_instructions text NOT NULL DEFAULT '',
    updated_by uuid NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_workspace_styles PRIMARY KEY (id),
    CONSTRAINT ck_workspace_styles_tone CHECK (
        tone IN ('neutral', 'formal', 'friendly', 'technical')),
    CONSTRAINT ck_workspace_styles_format CHECK (
        answer_format IN ('concise', 'detailed', 'bullets', 'table')),
    CONSTRAINT ck_workspace_styles_citation CHECK (citation_style IN ('numeric', 'inline')),
    CONSTRAINT ck_workspace_styles_language CHECK (
        language IS NULL OR length(language) BETWEEN 2 AND 40),
    CONSTRAINT ck_workspace_styles_custom CHECK (length(custom_instructions) <= 1000),
    CONSTRAINT fk_workspace_styles_org FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_workspace_styles_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_workspace_styles_updated_by FOREIGN KEY (updated_by) REFERENCES users (id)
);
CREATE UNIQUE INDEX uq_workspace_styles_workspace ON workspace_styles (workspace_id)
    WHERE workspace_id IS NOT NULL;
CREATE UNIQUE INDEX uq_workspace_styles_org_default ON workspace_styles (org_id)
    WHERE workspace_id IS NULL;
"""

_READ = (
    f"org_id = {CUR_ORG} AND (workspace_id IS NULL OR memory_ws_access(workspace_id) IS NOT NULL)"
)
_WRITE = (
    f"org_id = {CUR_ORG} AND (workspace_id IS NULL OR memory_ws_access(workspace_id) = 'admin')"
)
RLS_SQL = f"""
CREATE POLICY workspace_styles_select ON workspace_styles FOR SELECT USING ({_READ});
CREATE POLICY workspace_styles_insert ON workspace_styles FOR INSERT
    WITH CHECK ({_WRITE} AND updated_by = {CUR_USER});
CREATE POLICY workspace_styles_update ON workspace_styles FOR UPDATE
    USING ({_WRITE}) WITH CHECK ({_WRITE} AND updated_by = {CUR_USER});
"""


def upgrade() -> None:
    app = _role("app_role", "POSTGRES_APP_USER", "ukos_app")
    worker = _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")
    op.execute(SCHEMA_SQL)
    op.execute("ALTER TABLE workspace_styles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workspace_styles FORCE ROW LEVEL SECURITY")
    op.execute(RLS_SQL)
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON workspace_styles TO "{app}"')
    op.execute(f'GRANT SELECT ON workspace_styles TO "{worker}"')


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workspace_styles")
