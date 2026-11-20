"""Approved memory and bounded research sessions (M07.01, M07.02).

Contract: docs/architecture/m07-contracts.md sections 1-4.

* ``memories``: private to the owner by default; a ``visibility = 'workspace'`` memory is readable
  by workspace members once ``approved`` and by workspace admins in every state (they approve it).
  A suggestion is INSERTed only as ``suggested`` (policy). **No DELETE grant**: an owner deletes by
  setting ``deleted_at`` (soft delete; reads filter it). The app role may update only the approval
  / delete columns; invalidation and expiry go through the ``memory_*`` definer functions.
* ``research_sessions`` / ``research_steps`` / ``research_reports``: private to the creator, like
  chat runs (steps and reports inherit through an EXISTS on the session). The worker runs a session
  exactly like a chat run: ``research_claim_session`` (planned -> running, lease, epoch, attempts),
  fenced writes, ``research_reap_expired`` (requeue with a ``research.session.queued`` outbox row,
  or ``failed lease_expired`` once attempts are used), ``research_cancel`` (app, creator only).
* ``memory_ws_access(workspace_id)`` (definer, stable): ``'admin' | 'member' | NULL`` for the
  context user, so the policies work for the worker role without any membership grant.
* ``memory_invalidate_evidence(org, document_ids, reason)`` (app + worker) and ``memory_sweep()``
  (worker): evidence-change invalidation and expiry, across owners (the owner-private RLS would
  hide other users' rows from the caller).
* The outbox CHECK is extended with ``research.session.queued``.

Revision ID: 0010_memory_research
Revises: 0009_connectors
Create Date: 2026-11-20
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0010_memory_research"
down_revision: str | None = "0009_connectors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
CUR_USER = "nullif(current_setting('app.current_user_id', true), '')::uuid"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

TABLES = ("memories", "research_sessions", "research_steps", "research_reports")

FUNCTIONS = (
    "memory_ws_access(uuid)",
    "memory_invalidate_evidence(uuid, uuid[], text)",
    "memory_sweep()",
    "research_claim_session(uuid, text, integer)",
    "research_reap_expired(integer)",
    "research_cancel(uuid)",
)

OUTBOX_TYPES_BEFORE = (
    "'ingestion.job.queued', 'knowledge.revision.published', 'document.deleted', "
    "'knowledge.reconcile', 'chat.run.queued', 'source.sync.requested'"
)
OUTBOX_TYPES_AFTER = OUTBOX_TYPES_BEFORE + ", 'research.session.queued'"


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


SCHEMA_SQL = """
CREATE TABLE memories (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid,
    owner_id uuid NOT NULL,
    visibility text NOT NULL DEFAULT 'private',
    kind text NOT NULL,
    statement text NOT NULL,
    state text NOT NULL DEFAULT 'suggested',
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    source text NOT NULL DEFAULT 'user',
    source_ref uuid,
    expires_at timestamptz,
    approved_by uuid,
    approved_at timestamptz,
    invalidated_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    CONSTRAINT pk_memories PRIMARY KEY (id),
    CONSTRAINT ck_memories_visibility CHECK (visibility IN ('private', 'workspace')),
    CONSTRAINT ck_memories_kind CHECK (kind IN ('fact', 'preference', 'decision')),
    CONSTRAINT ck_memories_state CHECK (
        state IN ('suggested', 'approved', 'rejected', 'expired', 'invalidated')),
    CONSTRAINT ck_memories_source CHECK (source IN ('user', 'chat', 'research')),
    CONSTRAINT ck_memories_statement CHECK (length(statement) BETWEEN 1 AND 2000),
    CONSTRAINT ck_memories_evidence CHECK (
        jsonb_typeof(evidence) = 'array'
        AND (kind = 'preference' OR jsonb_array_length(evidence) >= 1)
    ),
    CONSTRAINT ck_memories_workspace_visibility CHECK (
        visibility = 'private' OR workspace_id IS NOT NULL),
    CONSTRAINT ck_memories_approved CHECK (state <> 'approved' OR approved_at IS NOT NULL),
    CONSTRAINT ck_memories_invalidated_reason CHECK (
        (state = 'invalidated') = (invalidated_reason IS NOT NULL)
        AND (invalidated_reason IS NULL OR invalidated_reason IN (
            'evidence_deleted', 'source_revoked', 'evidence_superseded'))
    ),
    CONSTRAINT fk_memories_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_memories_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_memories_owner_id FOREIGN KEY (owner_id) REFERENCES users (id),
    CONSTRAINT fk_memories_approved_by FOREIGN KEY (approved_by) REFERENCES users (id)
);
CREATE INDEX ix_memories_org_id_owner_id_created_at ON memories (org_id, owner_id, created_at, id);
CREATE INDEX ix_memories_workspace_id ON memories (workspace_id) WHERE workspace_id IS NOT NULL;
CREATE INDEX ix_memories_expires_at ON memories (expires_at)
    WHERE expires_at IS NOT NULL AND state IN ('suggested', 'approved');

CREATE TABLE research_sessions (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid,
    created_by uuid NOT NULL,
    question text NOT NULL,
    state text NOT NULL DEFAULT 'planned',
    plan jsonb NOT NULL DEFAULT '{}'::jsonb,
    limits jsonb NOT NULL DEFAULT '{}'::jsonb,
    usage jsonb NOT NULL DEFAULT '{}'::jsonb,
    scope_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_code text,
    attempts integer NOT NULL DEFAULT 0,
    lease_owner text,
    lease_expires_at timestamptz,
    lease_epoch bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    CONSTRAINT pk_research_sessions PRIMARY KEY (id),
    CONSTRAINT uq_research_sessions_id_org_id UNIQUE (id, org_id),
    CONSTRAINT ck_research_sessions_state CHECK (
        state IN ('planned', 'running', 'completed', 'failed', 'cancelled')),
    CONSTRAINT ck_research_sessions_question CHECK (length(question) BETWEEN 1 AND 2000),
    CONSTRAINT ck_research_sessions_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_research_sessions_lease_epoch CHECK (lease_epoch >= 0),
    CONSTRAINT ck_research_sessions_lease CHECK ((state = 'running') = (lease_owner IS NOT NULL)),
    CONSTRAINT ck_research_sessions_finished CHECK (
        (state IN ('completed', 'failed', 'cancelled')) = (finished_at IS NOT NULL)),
    CONSTRAINT ck_research_sessions_error_code CHECK (
        error_code IS NULL OR (state IN ('failed', 'cancelled') AND length(error_code) <= 64)),
    CONSTRAINT fk_research_sessions_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_research_sessions_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_research_sessions_created_by FOREIGN KEY (created_by) REFERENCES users (id)
);
CREATE INDEX ix_research_sessions_org_id_created_by_created_at
    ON research_sessions (org_id, created_by, created_at, id);
CREATE INDEX ix_research_sessions_running_lease ON research_sessions (lease_expires_at)
    WHERE state = 'running';

CREATE TABLE research_steps (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    session_id uuid NOT NULL,
    seq integer NOT NULL,
    kind text NOT NULL,
    query text,
    state text NOT NULL DEFAULT 'pending',
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz,
    finished_at timestamptz,
    CONSTRAINT pk_research_steps PRIMARY KEY (id),
    CONSTRAINT uq_research_steps_session_id_seq UNIQUE (session_id, seq),
    CONSTRAINT ck_research_steps_kind CHECK (kind IN ('plan', 'search', 'synthesize', 'report')),
    CONSTRAINT ck_research_steps_state CHECK (state IN ('pending', 'running', 'done', 'failed')),
    CONSTRAINT ck_research_steps_seq CHECK (seq >= 0),
    CONSTRAINT fk_research_steps_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_research_steps_session FOREIGN KEY (session_id, org_id)
        REFERENCES research_sessions (id, org_id)
);

CREATE TABLE research_reports (
    session_id uuid NOT NULL,
    org_id uuid NOT NULL,
    sections jsonb NOT NULL DEFAULT '[]'::jsonb,
    verification jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_research_reports PRIMARY KEY (session_id),
    CONSTRAINT ck_research_reports_sections CHECK (jsonb_typeof(sections) = 'array'),
    CONSTRAINT fk_research_reports_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_research_reports_session FOREIGN KEY (session_id, org_id)
        REFERENCES research_sessions (id, org_id)
);
"""

_SESSION_VISIBLE = (
    "EXISTS (SELECT 1 FROM research_sessions s WHERE s.id = {table}.session_id"
    " AND s.org_id = {table}.org_id)"
)
_WS_ACCESS = "memory_ws_access(memories.workspace_id)"
_WS_READ = (
    "memories.visibility = 'workspace' AND memories.workspace_id IS NOT NULL AND ("
    f"{_WS_ACCESS} = 'admin' OR (memories.state = 'approved' AND {_WS_ACCESS} IS NOT NULL))"
)
_WS_ADMIN = (
    "memories.visibility = 'workspace' AND memories.workspace_id IS NOT NULL"
    f" AND {_WS_ACCESS} = 'admin'"
)

RLS_SQL = f"""
CREATE POLICY memories_select ON memories FOR SELECT
    USING (org_id = {CUR_ORG} AND (owner_id = {CUR_USER} OR ({_WS_READ})));
CREATE POLICY memories_insert ON memories FOR INSERT
    WITH CHECK (org_id = {CUR_ORG} AND owner_id = {CUR_USER} AND state = 'suggested');
CREATE POLICY memories_update ON memories FOR UPDATE
    USING (org_id = {CUR_ORG} AND (owner_id = {CUR_USER} OR ({_WS_ADMIN})))
    WITH CHECK (org_id = {CUR_ORG} AND (owner_id = {CUR_USER} OR ({_WS_ADMIN})));

CREATE POLICY research_sessions_select ON research_sessions FOR SELECT
    USING (org_id = {CUR_ORG} AND created_by = {CUR_USER});
CREATE POLICY research_sessions_insert ON research_sessions FOR INSERT
    WITH CHECK (org_id = {CUR_ORG} AND created_by = {CUR_USER} AND state = 'planned');
CREATE POLICY research_sessions_update ON research_sessions FOR UPDATE
    USING (org_id = {CUR_ORG} AND created_by = {CUR_USER})
    WITH CHECK (org_id = {CUR_ORG} AND created_by = {CUR_USER});

CREATE POLICY research_steps_select ON research_steps FOR SELECT
    USING (org_id = {CUR_ORG} AND {_SESSION_VISIBLE.format(table="research_steps")});
CREATE POLICY research_steps_insert ON research_steps FOR INSERT
    WITH CHECK (org_id = {CUR_ORG} AND {_SESSION_VISIBLE.format(table="research_steps")});
CREATE POLICY research_steps_update ON research_steps FOR UPDATE
    USING (org_id = {CUR_ORG} AND {_SESSION_VISIBLE.format(table="research_steps")})
    WITH CHECK (org_id = {CUR_ORG} AND {_SESSION_VISIBLE.format(table="research_steps")});

CREATE POLICY research_reports_select ON research_reports FOR SELECT
    USING (org_id = {CUR_ORG} AND {_SESSION_VISIBLE.format(table="research_reports")});
CREATE POLICY research_reports_insert ON research_reports FOR INSERT
    WITH CHECK (org_id = {CUR_ORG} AND {_SESSION_VISIBLE.format(table="research_reports")});
"""

FUNCTIONS_SQL = """
-- The context user's standing in a workspace: 'admin' (workspace admin or org owner/admin),
-- 'member' (explicit membership), else NULL. Used by the memories policies (so the worker role
-- needs no membership grant).
CREATE FUNCTION memory_ws_access(p_workspace_id uuid)
RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_org uuid := nullif(current_setting('app.current_org_id', true), '')::uuid;
    v_user uuid := nullif(current_setting('app.current_user_id', true), '')::uuid;
    v_role text;
    v_ws_role text;
BEGIN
    IF p_workspace_id IS NULL OR v_org IS NULL OR v_user IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT m.role INTO v_role FROM public.organization_memberships m
        JOIN public.workspaces w ON w.id = p_workspace_id AND w.org_id = m.org_id
            AND w.deleted_at IS NULL
        WHERE m.org_id = v_org AND m.user_id = v_user;
    IF v_role IS NULL THEN
        RETURN NULL;
    END IF;
    IF v_role IN ('owner', 'admin') THEN
        RETURN 'admin';
    END IF;
    SELECT wm.role INTO v_ws_role FROM public.workspace_memberships wm
        WHERE wm.workspace_id = p_workspace_id AND wm.org_id = v_org AND wm.user_id = v_user;
    IF v_ws_role IS NULL THEN
        RETURN NULL;
    END IF;
    RETURN CASE WHEN v_ws_role = 'admin' THEN 'admin' ELSE 'member' END;
END;
$$;

-- Marks the org's live memories whose evidence names one of the documents ``invalidated``.
-- Across owners on purpose (a source revoke is done by an admin, the memories belong to others).
-- Inside a tenant transaction the org must be the context org.
CREATE FUNCTION memory_invalidate_evidence(p_org_id uuid, p_document_ids uuid[], p_reason text)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_ctx uuid := nullif(current_setting('app.current_org_id', true), '')::uuid;
    v_count integer;
BEGIN
    IF p_org_id IS NULL OR p_document_ids IS NULL OR cardinality(p_document_ids) = 0 THEN
        RETURN 0;
    END IF;
    IF p_reason NOT IN ('evidence_deleted', 'source_revoked', 'evidence_superseded') THEN
        RAISE EXCEPTION 'memory_invalidate_evidence: invalid reason' USING ERRCODE = '22023';
    END IF;
    IF v_ctx IS NOT NULL AND v_ctx <> p_org_id THEN
        RAISE EXCEPTION 'memory_invalidate_evidence: org mismatch' USING ERRCODE = '42501';
    END IF;
    UPDATE public.memories m SET
        state = 'invalidated', invalidated_reason = p_reason, updated_at = now()
    WHERE m.org_id = p_org_id AND m.state IN ('suggested', 'approved')
      AND EXISTS (
        SELECT 1 FROM jsonb_array_elements(m.evidence) e
        WHERE (e ->> 'document_id')::uuid = ANY (p_document_ids));
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$;

-- Maintenance (dispatcher): expiry, then evidence deleted (document or workspace gone) and
-- evidence superseded (the published version is no longer the cited one).
CREATE FUNCTION memory_sweep()
RETURNS TABLE (expired integer, invalidated integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_expired integer;
    v_deleted integer;
    v_superseded integer;
BEGIN
    UPDATE public.memories m SET state = 'expired', updated_at = now()
    WHERE m.state IN ('suggested', 'approved') AND m.expires_at IS NOT NULL
      AND m.expires_at < now();
    GET DIAGNOSTICS v_expired = ROW_COUNT;

    UPDATE public.memories m SET
        state = 'invalidated', invalidated_reason = 'evidence_deleted', updated_at = now()
    WHERE m.state IN ('suggested', 'approved') AND EXISTS (
        SELECT 1 FROM jsonb_array_elements(m.evidence) e
        JOIN public.documents d ON d.id = (e ->> 'document_id')::uuid AND d.org_id = m.org_id
        LEFT JOIN public.workspaces w ON w.id = d.workspace_id AND w.org_id = d.org_id
        WHERE d.deleted_at IS NOT NULL OR w.deleted_at IS NOT NULL);
    GET DIAGNOSTICS v_deleted = ROW_COUNT;

    UPDATE public.memories m SET
        state = 'invalidated', invalidated_reason = 'evidence_superseded', updated_at = now()
    WHERE m.state IN ('suggested', 'approved') AND EXISTS (
        SELECT 1 FROM jsonb_array_elements(m.evidence) e
        JOIN public.document_publications p ON p.document_id = (e ->> 'document_id')::uuid
            AND p.org_id = m.org_id
        WHERE p.version_id <> (e ->> 'version_id')::uuid);
    GET DIAGNOSTICS v_superseded = ROW_COUNT;
    RETURN QUERY SELECT v_expired, v_deleted + v_superseded;
END;
$$;

CREATE FUNCTION research_claim_session(p_id uuid, p_owner text, p_lease_seconds integer)
RETURNS TABLE (
    session_id uuid, org_id uuid, created_by uuid, attempts integer, lease_epoch bigint
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_row public.research_sessions%ROWTYPE;
BEGIN
    IF p_owner IS NULL OR length(p_owner) = 0 OR p_lease_seconds IS NULL OR p_lease_seconds < 1 THEN
        RAISE EXCEPTION 'research_claim_session: owner and a positive lease are required'
            USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_row FROM public.research_sessions r
        WHERE r.id = p_id AND r.state = 'planned'
        FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    UPDATE public.research_sessions r SET
        state = 'running',
        lease_owner = p_owner,
        lease_expires_at = now() + make_interval(secs => p_lease_seconds),
        lease_epoch = v_row.lease_epoch + 1,
        attempts = v_row.attempts + 1,
        started_at = COALESCE(r.started_at, now())
    WHERE r.id = v_row.id;
    RETURN QUERY SELECT v_row.id, v_row.org_id, v_row.created_by, v_row.attempts + 1,
        v_row.lease_epoch + 1;
END;
$$;

-- A dead worker's session goes back to ``planned`` with a fresh outbox row while attempts remain
-- (steps are persisted: the next worker resumes at the first step that is not ``done``); after
-- that it ends ``failed`` with ``lease_expired``.
CREATE FUNCTION research_reap_expired(p_max_attempts integer)
RETURNS TABLE (session_id uuid, org_id uuid, outcome text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_row public.research_sessions%ROWTYPE;
BEGIN
    IF p_max_attempts IS NULL OR p_max_attempts < 1 THEN
        RAISE EXCEPTION 'research_reap_expired: max attempts must be >= 1' USING ERRCODE = '22023';
    END IF;
    FOR v_row IN
        SELECT * FROM public.research_sessions r
        WHERE r.state = 'running' AND r.lease_expires_at < now()
        ORDER BY r.lease_expires_at
        FOR UPDATE SKIP LOCKED
    LOOP
        IF v_row.attempts < p_max_attempts THEN
            UPDATE public.research_sessions r SET
                state = 'planned', lease_owner = NULL, lease_expires_at = NULL
            WHERE r.id = v_row.id;
            INSERT INTO public.outbox_events
                (org_id, aggregate_type, aggregate_id, event_type, payload, available_at)
            VALUES (v_row.org_id, 'research_session', v_row.id, 'research.session.queued',
                    jsonb_build_object('session_id', v_row.id), now());
            RETURN QUERY SELECT v_row.id, v_row.org_id, 'requeued'::text;
        ELSE
            UPDATE public.research_sessions r SET
                state = 'failed', lease_owner = NULL, lease_expires_at = NULL,
                error_code = 'lease_expired', finished_at = now()
            WHERE r.id = v_row.id;
            RETURN QUERY SELECT v_row.id, v_row.org_id, 'failed'::text;
        END IF;
    END LOOP;
END;
$$;

-- Creator-only cancel (the app role has no UPDATE on sessions); idempotent on a finished session.
CREATE FUNCTION research_cancel(p_id uuid)
RETURNS TABLE (session_id uuid, state text, cancelled boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_org uuid := nullif(current_setting('app.current_org_id', true), '')::uuid;
    v_user uuid := nullif(current_setting('app.current_user_id', true), '')::uuid;
    v_row public.research_sessions%ROWTYPE;
BEGIN
    IF v_org IS NULL OR v_user IS NULL OR p_id IS NULL THEN
        RETURN;
    END IF;
    SELECT * INTO v_row FROM public.research_sessions r
        WHERE r.id = p_id AND r.org_id = v_org AND r.created_by = v_user
        FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF v_row.state IN ('planned', 'running') THEN
        UPDATE public.research_sessions r SET
            state = 'cancelled', lease_owner = NULL, lease_expires_at = NULL,
            finished_at = now()
        WHERE r.id = v_row.id;
        RETURN QUERY SELECT v_row.id, 'cancelled'::text, true;
    ELSE
        RETURN QUERY SELECT v_row.id, v_row.state, false;
    END IF;
END;
$$;
"""


def _app_grants_sql(role: str) -> str:
    return f"""
GRANT SELECT, INSERT ON memories, research_sessions TO "{role}";
GRANT SELECT ON research_steps, research_reports TO "{role}";
GRANT UPDATE (state, approved_by, approved_at, updated_at, deleted_at) ON memories TO "{role}";
"""


def _worker_grants_sql(role: str) -> str:
    return f"""
GRANT SELECT ON memories TO "{role}";
GRANT SELECT ON research_sessions, research_steps, research_reports TO "{role}";
GRANT INSERT ON research_steps, research_reports TO "{role}";
GRANT UPDATE (state, plan, usage, error_code, started_at, finished_at, lease_owner,
              lease_expires_at) ON research_sessions TO "{role}";
GRANT UPDATE (state, result, started_at, finished_at) ON research_steps TO "{role}";
"""


def upgrade() -> None:
    app, worker = _app_role(), _worker_role()
    op.execute(SCHEMA_SQL)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(FUNCTIONS_SQL)
    for signature in FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    # The memories policies call memory_ws_access, so both roles execute it (it only reports the
    # standing of the context user in the context org); PUBLIC may not.
    for signature in ("memory_ws_access(uuid)", "memory_invalidate_evidence(uuid, uuid[], text)"):
        op.execute(f'GRANT EXECUTE ON FUNCTION {signature} TO "{app}", "{worker}"')
    op.execute(f'GRANT EXECUTE ON FUNCTION research_cancel(uuid) TO "{app}"')
    for signature in (
        "memory_sweep()",
        "research_claim_session(uuid, text, integer)",
        "research_reap_expired(integer)",
    ):
        op.execute(f'GRANT EXECUTE ON FUNCTION {signature} TO "{worker}"')
    op.execute(RLS_SQL)
    op.execute("ALTER TABLE outbox_events DROP CONSTRAINT ck_outbox_events_type")
    op.execute(
        "ALTER TABLE outbox_events ADD CONSTRAINT ck_outbox_events_type"
        f" CHECK (event_type IN ({OUTBOX_TYPES_AFTER}))"
    )
    op.execute(_app_grants_sql(app))
    op.execute(_worker_grants_sql(worker))


def downgrade() -> None:
    for table in ("research_reports", "research_steps", "research_sessions", "memories"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("DELETE FROM outbox_events WHERE event_type = 'research.session.queued'")
    op.execute("ALTER TABLE outbox_events DROP CONSTRAINT ck_outbox_events_type")
    op.execute(
        "ALTER TABLE outbox_events ADD CONSTRAINT ck_outbox_events_type"
        f" CHECK (event_type IN ({OUTBOX_TYPES_BEFORE}))"
    )
