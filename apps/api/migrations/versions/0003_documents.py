"""Documents, versions, sources and upload sessions (M02.02.01).

Contract: docs/architecture/m02-contracts.md §3 (ADR-009 immutable versions, ADR-013, ADR-028).

* ``sources``: one ``kind='upload'`` source per workspace (partial unique index).
* ``documents``: soft delete (``deleted_at``/``deleted_by`` set together); ``latest_version_id``
  can only name a version of the SAME document through the composite FK
  ``(latest_version_id, id) -> document_versions (id, document_id)``, DEFERRABLE INITIALLY
  DEFERRED so a document and its first version can be inserted in either order in one transaction.
* ``document_versions``: IMMUTABLE. A BEFORE UPDATE trigger rejects every change except
  ``purged_at`` going NULL -> a timestamp (SQLSTATE 42501, for every role including the owner).
  The app role is granted ``UPDATE (purged_at)`` only.
* ``upload_sessions``: ``state`` moves forward only (``pending`` -> one terminal state).
* Composite FKs pin every row's ``workspace_id``/``org_id`` pair to a real workspace of that org,
  and a version's workspace to its document's workspace.
* RLS ENABLE + FORCE on all four tables, fail-closed per-command policies on
  ``app.current_org_id``; the app role has NO DELETE anywhere and column-level UPDATE only.
* ``uq_resource_grants_active``: unique active grants (DC-015 follow-up).
* All functions are SECURITY INVOKER with ``search_path = pg_catalog, public, pg_temp``.

Revision ID: 0003_documents
Revises: 0002_rls_hardening
Create Date: 2026-11-18
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0003_documents"
down_revision: str | None = "0002_rls_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SHA256 = "^[0-9a-f]{64}$"
CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"

TABLES = ("sources", "documents", "document_versions", "upload_sessions")


def _app_role() -> str:
    role = (
        context.config.attributes.get("app_role")
        or context.get_x_argument(as_dictionary=True).get("app_role")
        or os.environ.get("POSTGRES_APP_USER")
        or "ukos_app"
    )
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise ValueError("invalid app role name")
    return str(role)


SCHEMA_SQL = f"""
CREATE TABLE sources (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    config jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_sources PRIMARY KEY (id),
    CONSTRAINT uq_sources_id_workspace_id UNIQUE (id, workspace_id),
    CONSTRAINT ck_sources_kind CHECK (kind IN ('upload', 'url', 'connector')),
    CONSTRAINT ck_sources_name_length CHECK (length(name) BETWEEN 1 AND 255),
    CONSTRAINT fk_sources_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_sources_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_sources_created_by FOREIGN KEY (created_by) REFERENCES users (id)
);
-- Created lazily by the first upload; at most one per workspace.
CREATE UNIQUE INDEX uq_sources_workspace_id_upload ON sources (workspace_id) WHERE kind = 'upload';

CREATE TABLE documents (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    source_id uuid NOT NULL,
    title text NOT NULL,
    latest_version_id uuid,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    deleted_by uuid,
    CONSTRAINT pk_documents PRIMARY KEY (id),
    CONSTRAINT uq_documents_id_workspace_id UNIQUE (id, workspace_id),
    CONSTRAINT ck_documents_title CHECK (
        length(title) BETWEEN 1 AND 255 AND title !~ '^[[:space:]]|[[:space:]]$'
    ),
    CONSTRAINT ck_documents_deleted CHECK ((deleted_at IS NULL) = (deleted_by IS NULL)),
    CONSTRAINT fk_documents_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_documents_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_documents_source FOREIGN KEY (source_id, workspace_id)
        REFERENCES sources (id, workspace_id),
    CONSTRAINT fk_documents_created_by FOREIGN KEY (created_by) REFERENCES users (id),
    CONSTRAINT fk_documents_deleted_by FOREIGN KEY (deleted_by) REFERENCES users (id)
);
CREATE INDEX ix_documents_workspace_id_updated_at ON documents (workspace_id, updated_at, id);
CREATE INDEX ix_documents_workspace_id_created_at ON documents (workspace_id, created_at, id);
CREATE INDEX ix_documents_workspace_id_title ON documents (workspace_id, title, id);

CREATE TABLE document_versions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    document_id uuid NOT NULL,
    version_number integer NOT NULL,
    original_filename text NOT NULL,
    content_type text NOT NULL,
    size_bytes bigint NOT NULL,
    sha256 text NOT NULL,
    object_key text NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    purged_at timestamptz,
    CONSTRAINT pk_document_versions PRIMARY KEY (id),
    CONSTRAINT uq_document_versions_document_id_version_number
        UNIQUE (document_id, version_number),
    CONSTRAINT uq_document_versions_id_document_id UNIQUE (id, document_id),
    CONSTRAINT uq_document_versions_object_key UNIQUE (object_key),
    CONSTRAINT ck_document_versions_version_number CHECK (version_number >= 1),
    CONSTRAINT ck_document_versions_original_filename
        CHECK (length(original_filename) BETWEEN 1 AND 255),
    CONSTRAINT ck_document_versions_content_type CHECK (length(content_type) BETWEEN 1 AND 255),
    CONSTRAINT ck_document_versions_size_bytes CHECK (size_bytes >= 0),
    CONSTRAINT ck_document_versions_sha256 CHECK (sha256 ~ '{SHA256}'),
    CONSTRAINT ck_document_versions_object_key CHECK (length(object_key) > 0),
    CONSTRAINT fk_document_versions_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_document_versions_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_document_versions_document FOREIGN KEY (document_id, workspace_id)
        REFERENCES documents (id, workspace_id),
    CONSTRAINT fk_document_versions_created_by FOREIGN KEY (created_by) REFERENCES users (id)
);
CREATE INDEX ix_document_versions_org_id ON document_versions (org_id);

-- latest_version_id may only name a version of THE SAME document (no cross-document pointer).
ALTER TABLE documents ADD CONSTRAINT fk_documents_latest_version
    FOREIGN KEY (latest_version_id, id) REFERENCES document_versions (id, document_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE upload_sessions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    document_id uuid NOT NULL,
    version_id uuid NOT NULL,
    created_by uuid NOT NULL,
    filename text NOT NULL,
    content_type text NOT NULL,
    size_bytes bigint NOT NULL,
    sha256 text NOT NULL,
    object_key text NOT NULL,
    state text NOT NULL DEFAULT 'pending',
    failure_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    finalized_at timestamptz,
    CONSTRAINT pk_upload_sessions PRIMARY KEY (id),
    CONSTRAINT uq_upload_sessions_version_id UNIQUE (version_id),
    CONSTRAINT ck_upload_sessions_state
        CHECK (state IN ('pending', 'finalized', 'failed', 'expired')),
    CONSTRAINT ck_upload_sessions_filename CHECK (length(filename) BETWEEN 1 AND 255),
    CONSTRAINT ck_upload_sessions_content_type CHECK (length(content_type) BETWEEN 1 AND 255),
    CONSTRAINT ck_upload_sessions_size_bytes CHECK (size_bytes >= 1),
    CONSTRAINT ck_upload_sessions_sha256 CHECK (sha256 ~ '{SHA256}'),
    CONSTRAINT ck_upload_sessions_object_key CHECK (length(object_key) > 0),
    CONSTRAINT ck_upload_sessions_expires_after_created CHECK (expires_at > created_at),
    CONSTRAINT ck_upload_sessions_failure_code
        CHECK ((state = 'failed') = (failure_code IS NOT NULL)),
    CONSTRAINT ck_upload_sessions_finalized_at
        CHECK ((state = 'finalized') = (finalized_at IS NOT NULL)),
    CONSTRAINT fk_upload_sessions_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_upload_sessions_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_upload_sessions_created_by FOREIGN KEY (created_by) REFERENCES users (id)
);
CREATE INDEX ix_upload_sessions_org_id_state ON upload_sessions (org_id, state);
CREATE INDEX ix_upload_sessions_org_id_created_by_state
    ON upload_sessions (org_id, created_by, state);

-- DC-015 follow-up: at most one ACTIVE (not revoked) grant per (resource, grantee, permission).
CREATE UNIQUE INDEX uq_resource_grants_active ON resource_grants
    (resource_type, resource_id, grantee_type, grantee_id, permission) WHERE revoked_at IS NULL;
"""

TRIGGER_SQL = """
-- A version never changes after finalize; the only allowed transition is purged_at NULL -> ts
-- (set once by the purge job). Applies to EVERY role, including the table owner.
CREATE FUNCTION ukos_document_versions_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF (NEW.id, NEW.org_id, NEW.workspace_id, NEW.document_id, NEW.version_number,
        NEW.original_filename, NEW.content_type, NEW.size_bytes, NEW.sha256, NEW.object_key,
        NEW.created_by, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.org_id, OLD.workspace_id, OLD.document_id, OLD.version_number,
        OLD.original_filename, OLD.content_type, OLD.size_bytes, OLD.sha256, OLD.object_key,
        OLD.created_by, OLD.created_at) THEN
        RAISE EXCEPTION 'document_versions rows are immutable (only purged_at may be set)'
            USING ERRCODE = '42501', CONSTRAINT = 'document_versions_immutable';
    END IF;
    IF OLD.purged_at IS NOT NULL AND NEW.purged_at IS DISTINCT FROM OLD.purged_at THEN
        RAISE EXCEPTION 'document_versions.purged_at cannot change once set'
            USING ERRCODE = '42501', CONSTRAINT = 'document_versions_immutable';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION ukos_document_versions_immutable() FROM PUBLIC;

CREATE TRIGGER document_versions_immutable BEFORE UPDATE ON document_versions
    FOR EACH ROW EXECUTE FUNCTION ukos_document_versions_immutable();

-- Upload session state is forward-only: once it left 'pending' its outcome is frozen.
CREATE FUNCTION ukos_upload_sessions_forward_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF OLD.state <> 'pending'
       AND (NEW.state, NEW.failure_code, NEW.finalized_at)
           IS DISTINCT FROM (OLD.state, OLD.failure_code, OLD.finalized_at) THEN
        RAISE EXCEPTION 'upload_sessions.state only moves forward (% -> % rejected)',
            OLD.state, NEW.state
            USING ERRCODE = '23514', CONSTRAINT = 'upload_sessions_state_forward_only';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION ukos_upload_sessions_forward_only() FROM PUBLIC;

CREATE TRIGGER upload_sessions_forward_only BEFORE UPDATE ON upload_sessions
    FOR EACH ROW EXECUTE FUNCTION ukos_upload_sessions_forward_only();
"""

# Per-command policies, only for the commands the app role is granted.
RLS_SQL = f"""
CREATE POLICY sources_select ON sources FOR SELECT USING (org_id = {CUR_ORG});
CREATE POLICY sources_insert ON sources FOR INSERT WITH CHECK (org_id = {CUR_ORG});

CREATE POLICY documents_select ON documents FOR SELECT USING (org_id = {CUR_ORG});
CREATE POLICY documents_insert ON documents FOR INSERT WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY documents_update ON documents FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});

CREATE POLICY document_versions_select ON document_versions FOR SELECT
    USING (org_id = {CUR_ORG});
CREATE POLICY document_versions_insert ON document_versions FOR INSERT
    WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY document_versions_update ON document_versions FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});

CREATE POLICY upload_sessions_select ON upload_sessions FOR SELECT USING (org_id = {CUR_ORG});
CREATE POLICY upload_sessions_insert ON upload_sessions FOR INSERT
    WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY upload_sessions_update ON upload_sessions FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});
"""


def _grants_sql(role: str) -> str:
    return f"""
GRANT SELECT, INSERT ON sources, documents, document_versions, upload_sessions TO "{role}";
GRANT UPDATE (title, latest_version_id, updated_at, deleted_at, deleted_by)
    ON documents TO "{role}";
GRANT UPDATE (purged_at) ON document_versions TO "{role}";
GRANT UPDATE (state, failure_code, finalized_at) ON upload_sessions TO "{role}";
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)
    op.execute(TRIGGER_SQL)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(RLS_SQL)
    op.execute(_grants_sql(_app_role()))


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_resource_grants_active")
    # documents <-> document_versions reference each other: break the cycle first. Dropping a
    # table then drops its own policies, triggers, indexes and grants.
    op.execute("ALTER TABLE documents DROP CONSTRAINT IF EXISTS fk_documents_latest_version")
    for table in ("upload_sessions", "document_versions", "documents", "sources"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP FUNCTION IF EXISTS ukos_upload_sessions_forward_only()")
    op.execute("DROP FUNCTION IF EXISTS ukos_document_versions_immutable()")
