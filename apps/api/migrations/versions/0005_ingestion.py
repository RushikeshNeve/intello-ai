"""Reliable knowledge ingestion: jobs, events, checkpoints, revisions, publications, outbox.

M03.01.01.

Contract: docs/architecture/m03-contracts.md §3 and §4 (ADR-013, ADR-015, ADR-016, ADR-036).

* Six org-scoped tables, composite FKs pin every row's ``org_id``/``workspace_id`` to a real
  workspace and a version to its document. ENABLE + FORCE RLS with fail-closed per-command
  policies on ``app.current_org_id`` (no ``TO`` clause, so they bind every role). **No DELETE grant
  to anyone**; column-limited UPDATE where an update is allowed at all.
* ``ingestion_job_events`` is append-only: a trigger rejects UPDATE/DELETE/TRUNCATE for every role
  (SQLSTATE 42501).
* New role ``ukos_worker`` (``-x worker_role=`` / ``POSTGRES_WORKER_USER``; password from
  ``-x worker_password=`` / ``POSTGRES_WORKER_PASSWORD``): LOGIN NOSUPERUSER NOBYPASSRLS. It is
  created here (not by the postgres init script, which only runs on an empty volume). An existing
  role keeps its password. It discovers work across orgs ONLY through six SECURITY DEFINER
  functions; every other query runs under the normal RLS policies once it has set
  ``app.current_org_id``.
* The definer functions are owned by the migration user, pin ``search_path = pg_catalog, public,
  pg_temp``, qualify every relation, are revoked from PUBLIC and executable by the worker role only.
  They rely on the owner bypassing RLS (the bootstrap superuser: test_owner_is_superuser_caveat).

Revision ID: 0005_ingestion
Revises: 0004_upload_session_title
Create Date: 2026-11-19
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op
from sqlalchemy import text

revision: str = "0005_ingestion"
down_revision: str | None = "0004_upload_session_title"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SHA256 = "^[0-9a-f]{64}$"
CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

TABLES = (
    "ingestion_jobs",
    "ingestion_job_events",
    "knowledge_revisions",
    "ingestion_checkpoints",
    "document_publications",
    "outbox_events",
)

FUNCTIONS = (
    "ingest_claim_job(uuid, text, integer)",
    "ingest_reap_expired_leases(integer)",
    "outbox_fetch_batch(integer)",
    "outbox_mark_dispatched(uuid[])",
    "outbox_mark_failed(uuid, text, integer)",
    "knowledge_next_reconcile_batch(integer, integer)",
)

STAGES = "('extract', 'chunk', 'embed', 'index', 'validate', 'publish')"


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


def _app_role() -> str:
    return _role("app_role", "POSTGRES_APP_USER", "ukos_app")


def _worker_role() -> str:
    return _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")


def _worker_password() -> str | None:
    return (
        context.get_x_argument(as_dictionary=True).get("worker_password")
        or os.environ.get("POSTGRES_WORKER_PASSWORD")
        or None
    )


SCHEMA_SQL = f"""
CREATE TABLE ingestion_jobs (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    document_id uuid NOT NULL,
    version_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    pipeline_version text NOT NULL,
    embedding_space_id text NOT NULL,
    state text NOT NULL DEFAULT 'queued',
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 5,
    run_after timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_expires_at timestamptz,
    lease_epoch bigint NOT NULL DEFAULT 0,
    heartbeat_at timestamptz,
    current_stage text,
    last_error_code text,
    last_error_detail text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    CONSTRAINT pk_ingestion_jobs PRIMARY KEY (id),
    CONSTRAINT uq_ingestion_jobs_revision_id UNIQUE (revision_id),
    CONSTRAINT uq_ingestion_jobs_id_org_id UNIQUE (id, org_id),
    CONSTRAINT ck_ingestion_jobs_state CHECK (
        state IN ('queued', 'running', 'succeeded', 'dead', 'unsupported', 'cancelled')
    ),
    CONSTRAINT ck_ingestion_jobs_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_ingestion_jobs_max_attempts CHECK (max_attempts >= 1),
    CONSTRAINT ck_ingestion_jobs_lease_epoch CHECK (lease_epoch >= 0),
    CONSTRAINT ck_ingestion_jobs_lease CHECK ((state = 'running') = (lease_owner IS NOT NULL)),
    CONSTRAINT ck_ingestion_jobs_current_stage
        CHECK (current_stage IS NULL OR current_stage IN {STAGES}),
    CONSTRAINT ck_ingestion_jobs_last_error_detail
        CHECK (last_error_detail IS NULL OR length(last_error_detail) <= 500),
    CONSTRAINT fk_ingestion_jobs_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_ingestion_jobs_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_ingestion_jobs_document FOREIGN KEY (document_id, workspace_id)
        REFERENCES documents (id, workspace_id),
    CONSTRAINT fk_ingestion_jobs_version FOREIGN KEY (version_id, document_id)
        REFERENCES document_versions (id, document_id)
);
CREATE INDEX ix_ingestion_jobs_state_run_after ON ingestion_jobs (state, run_after);
CREATE INDEX ix_ingestion_jobs_org_id_document_id ON ingestion_jobs (org_id, document_id);

CREATE TABLE ingestion_job_events (
    seq bigint GENERATED ALWAYS AS IDENTITY,
    job_id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    document_id uuid NOT NULL,
    version_id uuid NOT NULL,
    event_type text NOT NULL,
    stage text,
    attempt integer,
    detail jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_ingestion_job_events PRIMARY KEY (seq),
    CONSTRAINT ck_ingestion_job_events_type CHECK (event_type IN (
        'queued', 'started', 'stage_started', 'stage_completed', 'retry_scheduled',
        'succeeded', 'dead', 'unsupported', 'cancelled', 'published'
    )),
    CONSTRAINT ck_ingestion_job_events_stage CHECK (stage IS NULL OR stage IN {STAGES}),
    CONSTRAINT ck_ingestion_job_events_attempt CHECK (attempt IS NULL OR attempt >= 0),
    CONSTRAINT fk_ingestion_job_events_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_ingestion_job_events_job FOREIGN KEY (job_id, org_id)
        REFERENCES ingestion_jobs (id, org_id),
    CONSTRAINT fk_ingestion_job_events_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id),
    CONSTRAINT fk_ingestion_job_events_document FOREIGN KEY (document_id, workspace_id)
        REFERENCES documents (id, workspace_id),
    CONSTRAINT fk_ingestion_job_events_version FOREIGN KEY (version_id, document_id)
        REFERENCES document_versions (id, document_id)
);
CREATE INDEX ix_ingestion_job_events_job_id ON ingestion_job_events (job_id);
CREATE INDEX ix_ingestion_job_events_org_id_document_id
    ON ingestion_job_events (org_id, document_id, seq);

CREATE TABLE knowledge_revisions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    document_id uuid NOT NULL,
    version_id uuid NOT NULL,
    pipeline_version text NOT NULL,
    embedding_space_id text NOT NULL,
    state text NOT NULL DEFAULT 'building',
    chunk_count integer NOT NULL DEFAULT 0,
    content_fingerprint text,
    created_at timestamptz NOT NULL DEFAULT now(),
    ready_at timestamptz,
    published_at timestamptz,
    superseded_at timestamptz,
    purged_at timestamptz,
    CONSTRAINT pk_knowledge_revisions PRIMARY KEY (id),
    CONSTRAINT uq_knowledge_revisions_version_pipeline_space
        UNIQUE (version_id, pipeline_version, embedding_space_id),
    CONSTRAINT uq_knowledge_revisions_id_org_id UNIQUE (id, org_id),
    CONSTRAINT uq_knowledge_revisions_id_document_id UNIQUE (id, document_id),
    CONSTRAINT ck_knowledge_revisions_state CHECK (
        state IN ('building', 'ready', 'published', 'superseded', 'failed', 'purged')
    ),
    CONSTRAINT ck_knowledge_revisions_chunk_count CHECK (chunk_count >= 0),
    CONSTRAINT ck_knowledge_revisions_content_fingerprint
        CHECK (content_fingerprint IS NULL OR content_fingerprint ~ '{SHA256}'),
    CONSTRAINT fk_knowledge_revisions_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_knowledge_revisions_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_knowledge_revisions_document FOREIGN KEY (document_id, workspace_id)
        REFERENCES documents (id, workspace_id),
    CONSTRAINT fk_knowledge_revisions_version FOREIGN KEY (version_id, document_id)
        REFERENCES document_versions (id, document_id)
);
CREATE INDEX ix_knowledge_revisions_org_id_document_id ON knowledge_revisions (org_id, document_id);

-- org_id is carried (not in the contract's column list) so the row is org-scoped for RLS.
CREATE TABLE ingestion_checkpoints (
    revision_id uuid NOT NULL,
    stage text NOT NULL,
    org_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'running',
    input_fingerprint text NOT NULL,
    artifact_key text,
    artifact_sha256 text,
    artifact_bytes bigint,
    attempts integer NOT NULL DEFAULT 0,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    error_code text,
    CONSTRAINT pk_ingestion_checkpoints PRIMARY KEY (revision_id, stage),
    CONSTRAINT ck_ingestion_checkpoints_stage CHECK (stage IN {STAGES}),
    CONSTRAINT ck_ingestion_checkpoints_state
        CHECK (state IN ('running', 'completed', 'failed')),
    CONSTRAINT ck_ingestion_checkpoints_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_ingestion_checkpoints_artifact_bytes
        CHECK (artifact_bytes IS NULL OR artifact_bytes >= 0),
    CONSTRAINT ck_ingestion_checkpoints_artifact_sha256
        CHECK (artifact_sha256 IS NULL OR artifact_sha256 ~ '{SHA256}'),
    CONSTRAINT ck_ingestion_checkpoints_completed_at
        CHECK ((state = 'completed') = (completed_at IS NOT NULL)),
    CONSTRAINT fk_ingestion_checkpoints_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_ingestion_checkpoints_revision FOREIGN KEY (revision_id, org_id)
        REFERENCES knowledge_revisions (id, org_id) ON DELETE CASCADE
);
CREATE INDEX ix_ingestion_checkpoints_org_id ON ingestion_checkpoints (org_id);

-- The publication pointer (ADR-015): at most one published revision per document.
CREATE TABLE document_publications (
    document_id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    version_id uuid NOT NULL,
    version_number integer NOT NULL,
    previous_revision_id uuid,
    published_at timestamptz NOT NULL DEFAULT now(),
    reconciled_at timestamptz,
    CONSTRAINT pk_document_publications PRIMARY KEY (document_id),
    CONSTRAINT ck_document_publications_version_number CHECK (version_number >= 1),
    CONSTRAINT fk_document_publications_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_document_publications_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_document_publications_document FOREIGN KEY (document_id, workspace_id)
        REFERENCES documents (id, workspace_id),
    CONSTRAINT fk_document_publications_version FOREIGN KEY (version_id, document_id)
        REFERENCES document_versions (id, document_id),
    CONSTRAINT fk_document_publications_revision FOREIGN KEY (revision_id, document_id)
        REFERENCES knowledge_revisions (id, document_id),
    CONSTRAINT fk_document_publications_previous_revision
        FOREIGN KEY (previous_revision_id, document_id)
        REFERENCES knowledge_revisions (id, document_id)
);
CREATE INDEX ix_document_publications_reconciled_at ON document_publications (reconciled_at);

CREATE TABLE outbox_events (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    seq bigint GENERATED ALWAYS AS IDENTITY,
    org_id uuid NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    available_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    dispatched_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    CONSTRAINT pk_outbox_events PRIMARY KEY (id),
    CONSTRAINT uq_outbox_events_seq UNIQUE (seq),
    CONSTRAINT ck_outbox_events_type CHECK (event_type IN (
        'ingestion.job.queued', 'knowledge.revision.published', 'document.deleted',
        'knowledge.reconcile'
    )),
    CONSTRAINT ck_outbox_events_aggregate_type CHECK (length(aggregate_type) > 0),
    CONSTRAINT ck_outbox_events_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_outbox_events_last_error
        CHECK (last_error IS NULL OR length(last_error) <= 500),
    CONSTRAINT fk_outbox_events_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE
);
CREATE INDEX ix_outbox_events_undispatched ON outbox_events (available_at, seq)
    WHERE dispatched_at IS NULL;
"""

TRIGGER_SQL = """
-- ingestion_job_events is append-only for every role, including the table owner.
CREATE FUNCTION ukos_ingestion_job_events_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'ingestion_job_events is append-only (% rejected)', TG_OP
        USING ERRCODE = '42501', CONSTRAINT = 'ingestion_job_events_append_only';
END;
$$;
REVOKE ALL ON FUNCTION ukos_ingestion_job_events_append_only() FROM PUBLIC;

CREATE TRIGGER ingestion_job_events_append_only
    BEFORE UPDATE OR DELETE ON ingestion_job_events
    FOR EACH ROW EXECUTE FUNCTION ukos_ingestion_job_events_append_only();
CREATE TRIGGER ingestion_job_events_no_truncate
    BEFORE TRUNCATE ON ingestion_job_events
    FOR EACH STATEMENT EXECUTE FUNCTION ukos_ingestion_job_events_append_only();
"""

# Per-command policies, only for the commands some role is granted.
RLS_SQL = f"""
CREATE POLICY ingestion_jobs_select ON ingestion_jobs FOR SELECT USING (org_id = {CUR_ORG});
CREATE POLICY ingestion_jobs_insert ON ingestion_jobs FOR INSERT WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY ingestion_jobs_update ON ingestion_jobs FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});

CREATE POLICY ingestion_job_events_select ON ingestion_job_events FOR SELECT
    USING (org_id = {CUR_ORG});
CREATE POLICY ingestion_job_events_insert ON ingestion_job_events FOR INSERT
    WITH CHECK (org_id = {CUR_ORG});

CREATE POLICY ingestion_checkpoints_select ON ingestion_checkpoints FOR SELECT
    USING (org_id = {CUR_ORG});
CREATE POLICY ingestion_checkpoints_insert ON ingestion_checkpoints FOR INSERT
    WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY ingestion_checkpoints_update ON ingestion_checkpoints FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});

CREATE POLICY knowledge_revisions_select ON knowledge_revisions FOR SELECT
    USING (org_id = {CUR_ORG});
CREATE POLICY knowledge_revisions_insert ON knowledge_revisions FOR INSERT
    WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY knowledge_revisions_update ON knowledge_revisions FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});

CREATE POLICY document_publications_select ON document_publications FOR SELECT
    USING (org_id = {CUR_ORG});
CREATE POLICY document_publications_insert ON document_publications FOR INSERT
    WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY document_publications_update ON document_publications FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});

CREATE POLICY outbox_events_select ON outbox_events FOR SELECT USING (org_id = {CUR_ORG});
CREATE POLICY outbox_events_insert ON outbox_events FOR INSERT WITH CHECK (org_id = {CUR_ORG});
"""

# SECURITY DEFINER functions: the worker's only way to look across orgs. They return ids and small
# scalars only (never document content). Every relation is schema-qualified; search_path is pinned
# with pg_temp last. ``#variable_conflict use_column`` resolves OUT-parameter/column name clashes.
FUNCTIONS_SQL = """
CREATE FUNCTION ingest_claim_job(p_job_id uuid, p_owner text, p_lease_seconds integer)
RETURNS TABLE (
    job_id uuid, org_id uuid, workspace_id uuid, document_id uuid, version_id uuid,
    revision_id uuid, attempts integer, lease_epoch bigint
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_job public.ingestion_jobs%ROWTYPE;
BEGIN
    IF p_owner IS NULL OR length(p_owner) = 0 OR p_lease_seconds IS NULL OR p_lease_seconds < 1 THEN
        RAISE EXCEPTION 'ingest_claim_job: owner and a positive lease are required'
            USING ERRCODE = '22023';
    END IF;
    -- Targeted by id; a row somebody else holds locked is skipped (duplicate delivery = no-op).
    SELECT * INTO v_job FROM public.ingestion_jobs j
        WHERE j.id = p_job_id AND j.state = 'queued' AND j.run_after <= now()
        FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    UPDATE public.ingestion_jobs j SET
        state = 'running',
        lease_owner = p_owner,
        lease_expires_at = now() + make_interval(secs => p_lease_seconds),
        lease_epoch = v_job.lease_epoch + 1,
        attempts = v_job.attempts + 1,
        heartbeat_at = now(),
        started_at = COALESCE(j.started_at, now()),
        updated_at = now()
    WHERE j.id = v_job.id;
    INSERT INTO public.ingestion_job_events
        (job_id, org_id, workspace_id, document_id, version_id, event_type, attempt, detail)
    VALUES (v_job.id, v_job.org_id, v_job.workspace_id, v_job.document_id, v_job.version_id,
            'started', v_job.attempts + 1,
            jsonb_build_object('lease_epoch', v_job.lease_epoch + 1));
    RETURN QUERY SELECT v_job.id, v_job.org_id, v_job.workspace_id, v_job.document_id,
        v_job.version_id, v_job.revision_id, v_job.attempts + 1, v_job.lease_epoch + 1;
END;
$$;

CREATE FUNCTION ingest_reap_expired_leases(p_backoff_base integer)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_job public.ingestion_jobs%ROWTYPE;
    v_count integer := 0;
    v_backoff double precision;
    v_run_after timestamptz;
BEGIN
    IF p_backoff_base IS NULL OR p_backoff_base < 0 THEN
        RAISE EXCEPTION 'ingest_reap_expired_leases: backoff base must be >= 0'
            USING ERRCODE = '22023';
    END IF;
    FOR v_job IN
        SELECT * FROM public.ingestion_jobs j
        WHERE j.state = 'running' AND j.lease_expires_at < now()
        ORDER BY j.lease_expires_at
        FOR UPDATE SKIP LOCKED
    LOOP
        IF v_job.attempts >= v_job.max_attempts THEN
            UPDATE public.ingestion_jobs j SET
                state = 'dead', lease_owner = NULL, lease_expires_at = NULL,
                last_error_code = 'lease_expired', finished_at = now(), updated_at = now()
            WHERE j.id = v_job.id;
            INSERT INTO public.ingestion_job_events
                (job_id, org_id, workspace_id, document_id, version_id, event_type, attempt,
                 detail)
            VALUES (v_job.id, v_job.org_id, v_job.workspace_id, v_job.document_id,
                    v_job.version_id, 'dead', v_job.attempts,
                    jsonb_build_object('reason', 'lease_expired'));
        ELSE
            v_backoff := least(3600::double precision,
                p_backoff_base * power(2::double precision, least(v_job.attempts - 1, 20)));
            v_run_after := now() + make_interval(secs => v_backoff);
            UPDATE public.ingestion_jobs j SET
                state = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                run_after = v_run_after, last_error_code = 'lease_expired', updated_at = now()
            WHERE j.id = v_job.id;
            INSERT INTO public.ingestion_job_events
                (job_id, org_id, workspace_id, document_id, version_id, event_type, attempt,
                 detail)
            VALUES (v_job.id, v_job.org_id, v_job.workspace_id, v_job.document_id,
                    v_job.version_id, 'retry_scheduled', v_job.attempts,
                    jsonb_build_object('reason', 'lease_expired', 'backoff_seconds', v_backoff));
            INSERT INTO public.outbox_events
                (org_id, aggregate_type, aggregate_id, event_type, payload, available_at)
            VALUES (v_job.org_id, 'ingestion_job', v_job.id, 'ingestion.job.queued',
                    jsonb_build_object('job_id', v_job.id), v_run_after);
        END IF;
        v_count := v_count + 1;
    END LOOP;
    RETURN v_count;
END;
$$;

-- Rows stay locked until the CALLER's transaction ends, so two dispatchers never get the same row.
CREATE FUNCTION outbox_fetch_batch(p_limit integer)
RETURNS TABLE (
    id uuid, seq bigint, org_id uuid, aggregate_type text, aggregate_id uuid, event_type text,
    payload jsonb, attempts integer
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
BEGIN
    IF p_limit IS NULL OR p_limit < 1 THEN
        RAISE EXCEPTION 'outbox_fetch_batch: limit must be >= 1' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
        SELECT o.id, o.seq, o.org_id, o.aggregate_type, o.aggregate_id, o.event_type, o.payload,
               o.attempts
        FROM public.outbox_events o
        WHERE o.dispatched_at IS NULL AND o.available_at <= now()
        ORDER BY o.seq
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED;
END;
$$;

CREATE FUNCTION outbox_mark_dispatched(p_ids uuid[])
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_count integer;
BEGIN
    UPDATE public.outbox_events o SET dispatched_at = now()
    WHERE o.id = ANY(p_ids) AND o.dispatched_at IS NULL;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$;

CREATE FUNCTION outbox_mark_failed(p_id uuid, p_error text, p_retry_in_seconds integer)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_count integer;
BEGIN
    IF p_retry_in_seconds IS NULL OR p_retry_in_seconds < 0 THEN
        RAISE EXCEPTION 'outbox_mark_failed: retry delay must be >= 0' USING ERRCODE = '22023';
    END IF;
    UPDATE public.outbox_events o SET
        attempts = o.attempts + 1,
        last_error = left(COALESCE(p_error, ''), 500),
        available_at = now() + make_interval(secs => p_retry_in_seconds)
    WHERE o.id = p_id AND o.dispatched_at IS NULL;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count > 0;
END;
$$;

CREATE FUNCTION knowledge_next_reconcile_batch(p_limit integer, p_older_than_seconds integer)
RETURNS TABLE (org_id uuid, document_id uuid, revision_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_older_than_seconds IS NULL
       OR p_older_than_seconds < 0 THEN
        RAISE EXCEPTION 'knowledge_next_reconcile_batch: invalid arguments' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
        WITH due AS (
            SELECT p.document_id FROM public.document_publications p
            WHERE p.reconciled_at IS NULL
               OR p.reconciled_at < now() - make_interval(secs => p_older_than_seconds)
            ORDER BY p.reconciled_at NULLS FIRST, p.document_id
            LIMIT p_limit
            FOR UPDATE SKIP LOCKED
        ), marked AS (
            UPDATE public.document_publications p SET reconciled_at = now()
            FROM due WHERE p.document_id = due.document_id
            RETURNING p.org_id, p.document_id, p.revision_id
        )
        SELECT m.org_id, m.document_id, m.revision_id FROM marked m;
END;
$$;
"""


def _app_grants_sql(role: str) -> str:
    return f"""
GRANT SELECT, INSERT ON ingestion_jobs, ingestion_job_events, outbox_events TO "{role}";
GRANT SELECT ON ingestion_checkpoints, knowledge_revisions, document_publications TO "{role}";
"""


def _worker_grants_sql(role: str) -> str:
    return f"""
GRANT USAGE ON SCHEMA public TO "{role}";
-- Read-only context the pipeline needs once app.current_org_id is set.
GRANT SELECT ON documents, document_versions, workspaces, organizations TO "{role}";
-- The ``organizations_select`` policy contains an EXISTS over organization_memberships, evaluated
-- with the CALLER's privileges: reading organizations needs these two columns (never ``role``).
-- Under RLS they only expose the current org's member ids (no user context is ever set).
GRANT SELECT (org_id, user_id) ON organization_memberships TO "{role}";
-- Publication writes the audit event ``document.version.indexed`` (contract §9; INSERT only).
GRANT INSERT ON audit_events TO "{role}";

GRANT SELECT, INSERT ON ingestion_jobs, ingestion_job_events, outbox_events TO "{role}";
-- State machine columns only; identity, ownership, attempts, max_attempts and the fencing epoch
-- change through the definer functions (attempts, lease_epoch) or never.
GRANT UPDATE (state, run_after, lease_owner, lease_expires_at, heartbeat_at, current_stage,
              last_error_code, last_error_detail, updated_at, started_at, finished_at)
    ON ingestion_jobs TO "{role}";

GRANT SELECT, INSERT ON ingestion_checkpoints, knowledge_revisions, document_publications
    TO "{role}";
GRANT UPDATE (state, input_fingerprint, artifact_key, artifact_sha256, artifact_bytes, attempts,
              started_at, completed_at, error_code)
    ON ingestion_checkpoints TO "{role}";
GRANT UPDATE (state, chunk_count, content_fingerprint, ready_at, published_at, superseded_at,
              purged_at)
    ON knowledge_revisions TO "{role}";
GRANT UPDATE (revision_id, version_id, version_number, previous_revision_id, published_at,
              reconciled_at)
    ON document_publications TO "{role}";
"""


def _ensure_worker_role(role: str, app_role: str) -> None:
    bind = op.get_bind()
    if role == app_role:
        raise ValueError("the worker role must differ from the app role")
    if bind.execute(text("SELECT current_user")).scalar_one() == role:
        raise ValueError("the worker role must differ from the migration user")
    row = bind.execute(
        text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :r"), {"r": role}
    ).first()
    if row is not None:
        if row.rolsuper or row.rolbypassrls:
            raise RuntimeError(
                f"existing role {role!r} is SUPERUSER or BYPASSRLS; refusing to use it as the "
                "worker role (no role may bypass row-level security)"
            )
        return  # exists: its password is left alone
    password = _worker_password()
    if not password:
        raise RuntimeError(
            f"worker role {role!r} does not exist and no password was given: pass "
            "`-x worker_password=<password>` or set POSTGRES_WORKER_PASSWORD "
            "(api-migrate receives it from the environment contract)"
        )
    # Bind parameters carry the name and password; they never appear in the SQL text or the log.
    bind.execute(
        text(
            "SELECT set_config('ukos.worker_role', :r, true),"
            " set_config('ukos.worker_password', :p, true)"
        ),
        {"r": role, "p": password},
    )
    op.execute(
        """
DO $$
BEGIN
    EXECUTE format(
        'CREATE ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION '
        'PASSWORD %L',
        current_setting('ukos.worker_role'), current_setting('ukos.worker_password'));
END
$$"""
    )
    bind.execute(
        text(
            "SELECT set_config('ukos.worker_role', '', true),"
            " set_config('ukos.worker_password', '', true)"
        )
    )


def upgrade() -> None:
    worker = _worker_role()
    _ensure_worker_role(worker, _app_role())
    op.execute(SCHEMA_SQL)
    op.execute(TRIGGER_SQL)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(RLS_SQL)
    op.execute(FUNCTIONS_SQL)
    for signature in FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(_app_grants_sql(_app_role()))
    op.execute(_worker_grants_sql(worker))
    for signature in FUNCTIONS:
        op.execute(f'GRANT EXECUTE ON FUNCTION {signature} TO "{worker}"')


def downgrade() -> None:
    worker = _worker_role()
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    # Dropping a table drops its own policies, triggers, indexes and grants. Referencing tables
    # first: checkpoints/publications reference knowledge_revisions, events reference jobs.
    for table in (
        "outbox_events",
        "document_publications",
        "ingestion_checkpoints",
        "ingestion_job_events",
        "ingestion_jobs",
        "knowledge_revisions",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP FUNCTION IF EXISTS ukos_ingestion_job_events_append_only()")
    # The worker role itself stays (it may own connections or be shared), but loses what 0005
    # gave it on tables that outlive this migration.
    bind = op.get_bind()
    exists = bind.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": worker}).scalar()
    if exists:
        op.execute(
            "REVOKE ALL ON documents, document_versions, workspaces, organizations,"
            f' organization_memberships, audit_events FROM "{worker}"'
        )
        op.execute(f'REVOKE USAGE ON SCHEMA public FROM "{worker}"')
