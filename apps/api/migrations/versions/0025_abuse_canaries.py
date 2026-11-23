"""M14.07 abuse protection and canary documents.

* ``security_events``: ids and counts only (never query or document text). FORCE RLS by org; the
  API restricts reads to org owners/admins. Acknowledgement is a column-level UPDATE grant.
* ``canary_documents``: the admin-only canary flag, kept out of ``documents`` so no document API
  can ever expose it. ``abuse_settings``: per-org thresholds and action.

Revision ID: 0025_abuse_canaries
Revises: 0024_document_findings
Create Date: 2026-11-22
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0025_abuse_canaries"
down_revision: str | None = "0024_document_findings"
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
CREATE TABLE security_events (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid,
    kind text NOT NULL,
    severity text NOT NULL,
    counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    run_id uuid,
    document_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    acknowledged_at timestamptz,
    acknowledged_by uuid,
    CONSTRAINT pk_security_events PRIMARY KEY (id),
    CONSTRAINT ck_security_events_kind CHECK (kind IN ('abuse_anomaly', 'canary_hit')),
    CONSTRAINT ck_security_events_severity CHECK (severity IN ('low', 'medium', 'high')),
    CONSTRAINT fk_security_events_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE
);
CREATE INDEX ix_security_events_org_created ON security_events (org_id, created_at DESC);
CREATE TABLE canary_documents (
    org_id uuid NOT NULL,
    document_id uuid NOT NULL,
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_canary_documents PRIMARY KEY (org_id, document_id),
    CONSTRAINT fk_canary_documents_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_canary_documents_document_id FOREIGN KEY (document_id)
        REFERENCES documents (id) ON DELETE CASCADE
);
CREATE TABLE abuse_settings (
    org_id uuid NOT NULL,
    action text NOT NULL DEFAULT 'flag',
    window_seconds integer NOT NULL DEFAULT 300,
    block_seconds integer NOT NULL DEFAULT 900,
    max_search integer NOT NULL DEFAULT 120,
    max_chat integer NOT NULL DEFAULT 60,
    max_evidence_reads integer NOT NULL DEFAULT 200,
    max_export integer NOT NULL DEFAULT 20,
    max_distinct_docs integer NOT NULL DEFAULT 150,
    updated_by uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_abuse_settings PRIMARY KEY (org_id),
    CONSTRAINT ck_abuse_settings_action CHECK (action IN ('off', 'flag', 'throttle', 'block')),
    CONSTRAINT ck_abuse_settings_bounds CHECK (
        window_seconds BETWEEN 10 AND 86400 AND block_seconds BETWEEN 10 AND 86400
        AND max_search > 0 AND max_chat > 0 AND max_evidence_reads > 0 AND max_export > 0
        AND max_distinct_docs > 0),
    CONSTRAINT fk_abuse_settings_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE
);
"""


def upgrade() -> None:
    app = _role("app_role", "POSTGRES_APP_USER", "ukos_app")
    worker = _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")
    op.execute(SCHEMA_SQL)
    for table in ("security_events", "canary_documents", "abuse_settings"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_org ON {table} USING (org_id = {CUR_ORG})"
            f" WITH CHECK (org_id = {CUR_ORG})"
        )
    op.execute(f'GRANT SELECT, INSERT ON security_events TO "{app}", "{worker}"')
    op.execute(f'GRANT UPDATE (acknowledged_at, acknowledged_by) ON security_events TO "{app}"')
    op.execute(f'GRANT SELECT, INSERT, DELETE ON canary_documents TO "{app}"')
    op.execute(f'GRANT SELECT ON canary_documents TO "{worker}"')
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON abuse_settings TO "{app}"')
    op.execute(f'GRANT SELECT ON abuse_settings TO "{worker}"')


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS abuse_settings")
    op.execute("DROP TABLE IF EXISTS canary_documents")
    op.execute("DROP TABLE IF EXISTS security_events")
