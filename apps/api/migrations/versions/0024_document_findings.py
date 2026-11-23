"""M14.02 ingestion-time document scanning (docs/architecture/m14-contracts.md, section M14.02).

* ``document_findings``: what the scan stage found per document version (rule, severity, a
  page/offset location and at most a 40-char redacted preview; never document text). Readable by
  workspace admins (incl. org owner/admin); the worker role writes them (FORCE RLS like documents).
* ``document_scan_policies``: ``off | flag | quarantine`` per workspace, plus an org default
  (``workspace_id`` NULL). Absent = ``flag``.
* ``document_scan_releases``: an admin released a quarantined version; the re-queued job then
  skips the quarantine decision.
* ``knowledge_revisions.state`` gains ``quarantined`` (never indexed, never published).
* ``ingest_release_quarantine(version_id)`` (definer): quarantined revision -> ``building`` and its
  succeeded job -> ``queued`` (with event + outbox row), like ``ingest_requeue_job``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0024_document_findings"
down_revision: str | None = "0019c_shadow_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
CUR_USER = "nullif(current_setting('app.current_user_id', true), '')::uuid"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SIGNATURE = "ingest_release_quarantine(uuid)"


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
CREATE TABLE document_findings (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    document_id uuid NOT NULL,
    version_id uuid NOT NULL,
    rule_id text NOT NULL,
    severity text NOT NULL,
    location text NOT NULL,
    preview text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_document_findings PRIMARY KEY (id),
    CONSTRAINT uq_document_findings_rule_location UNIQUE (version_id, rule_id, location),
    CONSTRAINT ck_document_findings_severity CHECK (severity IN ('low', 'medium', 'high')),
    CONSTRAINT ck_document_findings_rule CHECK (length(rule_id) BETWEEN 1 AND 64),
    CONSTRAINT ck_document_findings_location CHECK (length(location) BETWEEN 1 AND 200),
    CONSTRAINT ck_document_findings_preview CHECK (length(preview) <= 40),
    CONSTRAINT fk_document_findings_org FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_document_findings_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_document_findings_version FOREIGN KEY (version_id, document_id)
        REFERENCES document_versions (id, document_id)
);
CREATE INDEX ix_document_findings_doc ON document_findings (org_id, document_id, version_id);

CREATE TABLE document_scan_policies (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid,
    mode text NOT NULL DEFAULT 'flag',
    updated_by uuid NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_document_scan_policies PRIMARY KEY (id),
    CONSTRAINT ck_document_scan_policies_mode CHECK (mode IN ('off', 'flag', 'quarantine')),
    CONSTRAINT fk_document_scan_policies_org FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_document_scan_policies_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_document_scan_policies_updated_by FOREIGN KEY (updated_by) REFERENCES users (id)
);
CREATE UNIQUE INDEX uq_document_scan_policies_workspace ON document_scan_policies (workspace_id)
    WHERE workspace_id IS NOT NULL;
CREATE UNIQUE INDEX uq_document_scan_policies_org ON document_scan_policies (org_id)
    WHERE workspace_id IS NULL;

CREATE TABLE document_scan_releases (
    version_id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    document_id uuid NOT NULL,
    released_by uuid NOT NULL,
    released_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_document_scan_releases PRIMARY KEY (version_id),
    CONSTRAINT fk_document_scan_releases_org FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_document_scan_releases_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_document_scan_releases_version FOREIGN KEY (version_id, document_id)
        REFERENCES document_versions (id, document_id),
    CONSTRAINT fk_document_scan_releases_by FOREIGN KEY (released_by) REFERENCES users (id)
);

ALTER TABLE knowledge_revisions DROP CONSTRAINT ck_knowledge_revisions_state;
ALTER TABLE knowledge_revisions ADD CONSTRAINT ck_knowledge_revisions_state CHECK (
    state IN ('building', 'ready', 'published', 'superseded', 'failed', 'purged', 'quarantined')
);
"""

ADMIN = "memory_ws_access(workspace_id) = 'admin'"


def _rls_sql(worker: str) -> str:
    return f"""
CREATE POLICY document_findings_admin_select ON document_findings FOR SELECT
    USING (org_id = {CUR_ORG} AND {ADMIN});
CREATE POLICY document_findings_worker ON document_findings FOR ALL TO "{worker}"
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});

CREATE POLICY document_scan_policies_select ON document_scan_policies FOR SELECT
    USING (org_id = {CUR_ORG}
           AND (workspace_id IS NULL OR memory_ws_access(workspace_id) IS NOT NULL));
CREATE POLICY document_scan_policies_insert ON document_scan_policies FOR INSERT
    WITH CHECK (org_id = {CUR_ORG} AND {ADMIN} AND updated_by = {CUR_USER});
CREATE POLICY document_scan_policies_update ON document_scan_policies FOR UPDATE
    USING (org_id = {CUR_ORG} AND {ADMIN})
    WITH CHECK (org_id = {CUR_ORG} AND {ADMIN} AND updated_by = {CUR_USER});
CREATE POLICY document_scan_policies_worker ON document_scan_policies FOR SELECT TO "{worker}"
    USING (org_id = {CUR_ORG});

CREATE POLICY document_scan_releases_select ON document_scan_releases FOR SELECT
    USING (org_id = {CUR_ORG} AND {ADMIN});
CREATE POLICY document_scan_releases_insert ON document_scan_releases FOR INSERT
    WITH CHECK (org_id = {CUR_ORG} AND {ADMIN} AND released_by = {CUR_USER});
CREATE POLICY document_scan_releases_worker ON document_scan_releases FOR SELECT TO "{worker}"
    USING (org_id = {CUR_ORG});
"""


FUNCTION_SQL = """
CREATE FUNCTION ingest_release_quarantine(p_version_id uuid)
RETURNS TABLE (job_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_org uuid := nullif(current_setting('app.current_org_id', true), '')::uuid;
    v_job public.ingestion_jobs%ROWTYPE;
BEGIN
    IF v_org IS NULL OR p_version_id IS NULL THEN
        RETURN;
    END IF;
    SELECT j.* INTO v_job
    FROM public.ingestion_jobs j
    JOIN public.knowledge_revisions r ON r.id = j.revision_id AND r.org_id = j.org_id
    JOIN public.documents d ON d.id = j.document_id AND d.org_id = j.org_id
    WHERE j.version_id = p_version_id
      AND j.org_id = v_org
      AND d.deleted_at IS NULL
      AND j.state = 'succeeded'
      AND r.state = 'quarantined'
    ORDER BY j.created_at DESC
    LIMIT 1
    FOR UPDATE OF j;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    UPDATE public.ingestion_jobs j SET
        state = 'queued', attempts = 0, run_after = now(), lease_owner = NULL,
        lease_expires_at = NULL, heartbeat_at = NULL, current_stage = NULL,
        last_error_code = NULL, last_error_detail = NULL, finished_at = NULL, updated_at = now()
    WHERE j.id = v_job.id;
    UPDATE public.knowledge_revisions r SET state = 'building'
    WHERE r.id = v_job.revision_id AND r.org_id = v_org AND r.state = 'quarantined';
    INSERT INTO public.ingestion_job_events
        (job_id, org_id, workspace_id, document_id, version_id, event_type, attempt, detail)
    VALUES (v_job.id, v_job.org_id, v_job.workspace_id, v_job.document_id, v_job.version_id,
            'queued', 0, jsonb_build_object('reason', 'quarantine_released'));
    INSERT INTO public.outbox_events
        (org_id, aggregate_type, aggregate_id, event_type, payload, available_at)
    VALUES (v_job.org_id, 'ingestion_job', v_job.id, 'ingestion.job.queued',
            jsonb_build_object('job_id', v_job.id), now());
    RETURN QUERY SELECT v_job.id;
END;
$$;
"""

TABLES = ("document_findings", "document_scan_policies", "document_scan_releases")


def upgrade() -> None:
    app = _role("app_role", "POSTGRES_APP_USER", "ukos_app")
    worker = _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")
    op.execute(SCHEMA_SQL)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(_rls_sql(worker))
    op.execute(f'GRANT SELECT ON document_findings TO "{app}"')
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON document_scan_policies TO "{app}"')
    op.execute(f'GRANT SELECT, INSERT ON document_scan_releases TO "{app}"')
    op.execute(f'GRANT SELECT, INSERT, DELETE ON document_findings TO "{worker}"')
    op.execute(f'GRANT SELECT ON document_scan_policies, document_scan_releases TO "{worker}"')
    op.execute(FUNCTION_SQL)
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f'GRANT EXECUTE ON FUNCTION {SIGNATURE} TO "{app}"')


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SIGNATURE}")
    op.execute("DROP TABLE IF EXISTS document_scan_releases, document_scan_policies")
    op.execute("DROP TABLE IF EXISTS document_findings")
    op.execute("UPDATE knowledge_revisions SET state = 'failed' WHERE state = 'quarantined'")
    op.execute("ALTER TABLE knowledge_revisions DROP CONSTRAINT ck_knowledge_revisions_state")
    op.execute(
        "ALTER TABLE knowledge_revisions ADD CONSTRAINT ck_knowledge_revisions_state CHECK ("
        "state IN ('building', 'ready', 'published', 'superseded', 'failed', 'purged'))"
    )
