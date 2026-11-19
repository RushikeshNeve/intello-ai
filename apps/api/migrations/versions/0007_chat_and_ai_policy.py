"""Chat persistence and the per-org AI policy (M04.02.01 storage, M04.02.04 persistence).

Contract: docs/architecture/m04-contracts.md §5 and §8 (ADR-024, ADR-030, ADR-033, ADR-035).

* Six org-scoped tables: ``ai_policies``, ``chat_threads``, ``chat_messages``, ``chat_runs``,
  ``chat_run_events`` (and the outbox CHECK is extended with ``chat.run.queued``). Composite FKs pin
  every row's ``org_id`` to a real org, a message/run to its thread and an event to its run.
  ENABLE + FORCE RLS with fail-closed per-command policies on ``app.current_org_id`` (no ``TO``
  clause: they bind every role). **No DELETE grant to anyone**; column-limited UPDATE.
* A thread is **private to its creator**: the thread policy requires ``created_by =
  app.current_user_id`` as well, and messages, runs and events inherit that through an ``EXISTS`` on
  the thread/run (itself under RLS), so another member of the org sees nothing. A worker acting for
  a run therefore sets BOTH ``app.current_org_id`` and ``app.current_user_id`` (= the run's
  ``created_by``, returned by ``chat_claim_run``).
* ``chat_messages`` and ``chat_run_events`` are append-only (a trigger rejects
  UPDATE/DELETE/TRUNCATE for every role, SQLSTATE 42501).
* ``ai_policies``: members of the org read it; only an org ``owner``/``admin`` may insert/update it
  (checked in the policy against ``organization_memberships``, as defence in depth under the route).
* SECURITY DEFINER functions (owned by the migration user, ``search_path = pg_catalog, public,
  pg_temp``, every relation qualified, ``REVOKE ... FROM PUBLIC``; they rely on the owner bypassing
  RLS like 0005):

  - ``chat_claim_run(run_id, owner, lease_seconds)``: worker. queued -> running,
    ``lease_epoch + 1``,
    ``attempts + 1``, ``FOR UPDATE SKIP LOCKED`` (a duplicate delivery claims nothing);
  - ``chat_reap_expired_runs(max_attempts)``: worker. An expired lease requeues the run once when
    it produced no ``delta`` event and ``attempts < max_attempts``; otherwise it ends ``failed``
    with ``error_code = 'lease_expired'`` plus a ``failed`` event (a stream client terminates);
  - ``chat_cancel_run(run_id)``: app. Creator-only (``app.current_user_id`` and
    ``app.current_org_id``), queued/running -> ``cancelled`` + ``cancelled`` event; idempotent;
  - ``chat_readable_workspaces(org_id, user_id)``: worker. The live workspaces a user may read
    (contract m04 §2 derivation), so the worker re-resolves scope without any membership grant.

Revision ID: 0007_chat_and_ai_policy
Revises: 0006_ingestion_requeue
Create Date: 2026-11-19
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0007_chat_and_ai_policy"
down_revision: str | None = "0006_ingestion_requeue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
CUR_USER = "nullif(current_setting('app.current_user_id', true), '')::uuid"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

TABLES = ("ai_policies", "chat_threads", "chat_messages", "chat_runs", "chat_run_events")

FUNCTIONS = (
    "chat_claim_run(uuid, text, integer)",
    "chat_reap_expired_runs(integer)",
    "chat_cancel_run(uuid)",
    "chat_readable_workspaces(uuid, uuid)",
)

OUTBOX_TYPES_BEFORE = (
    "'ingestion.job.queued', 'knowledge.revision.published', 'document.deleted', "
    "'knowledge.reconcile'"
)
OUTBOX_TYPES_AFTER = OUTBOX_TYPES_BEFORE + ", 'chat.run.queued'"

RUN_STATES = "('queued', 'running', 'completed', 'failed', 'cancelled')"
EVENT_TYPES = (
    "('started', 'retrieval', 'delta', 'citations', 'verification', 'completed', 'failed', "
    "'cancelled')"
)


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
CREATE TABLE ai_policies (
    org_id uuid NOT NULL,
    allow_external boolean NOT NULL DEFAULT false,
    allowed_providers text[] NOT NULL DEFAULT ARRAY['local']::text[],
    updated_by uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_ai_policies PRIMARY KEY (org_id),
    CONSTRAINT ck_ai_policies_providers CHECK (
        cardinality(allowed_providers) >= 1
        AND allowed_providers <@ ARRAY['local', 'openai']::text[]
    ),
    -- an external provider can only be listed when external use is allowed at all
    CONSTRAINT ck_ai_policies_external CHECK (
        allow_external OR allowed_providers <@ ARRAY['local']::text[]
    ),
    CONSTRAINT fk_ai_policies_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_ai_policies_updated_by FOREIGN KEY (updated_by) REFERENCES users (id)
);

CREATE TABLE chat_threads (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid,
    created_by uuid NOT NULL,
    title text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    CONSTRAINT pk_chat_threads PRIMARY KEY (id),
    CONSTRAINT uq_chat_threads_id_org_id UNIQUE (id, org_id),
    CONSTRAINT ck_chat_threads_title CHECK (length(title) <= 200),
    CONSTRAINT fk_chat_threads_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_chat_threads_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_chat_threads_created_by FOREIGN KEY (created_by) REFERENCES users (id)
);
CREATE INDEX ix_chat_threads_org_id_created_by_created_at
    ON chat_threads (org_id, created_by, created_at, id);

CREATE TABLE chat_runs (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    user_message_id uuid NOT NULL,
    created_by uuid NOT NULL,
    state text NOT NULL DEFAULT 'queued',
    idempotency_key text NOT NULL,
    scope_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    model jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_code text,
    attempts integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    lease_owner text,
    lease_expires_at timestamptz,
    lease_epoch bigint NOT NULL DEFAULT 0,
    CONSTRAINT pk_chat_runs PRIMARY KEY (id),
    CONSTRAINT uq_chat_runs_thread_id_idempotency_key UNIQUE (thread_id, idempotency_key),
    CONSTRAINT uq_chat_runs_id_org_id UNIQUE (id, org_id),
    CONSTRAINT uq_chat_runs_id_thread_id UNIQUE (id, thread_id),
    CONSTRAINT ck_chat_runs_state CHECK (state IN {RUN_STATES}),
    CONSTRAINT ck_chat_runs_idempotency_key CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    CONSTRAINT ck_chat_runs_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_chat_runs_lease_epoch CHECK (lease_epoch >= 0),
    CONSTRAINT ck_chat_runs_lease CHECK ((state = 'running') = (lease_owner IS NOT NULL)),
    CONSTRAINT ck_chat_runs_finished CHECK (
        (state IN ('completed', 'failed', 'cancelled')) = (finished_at IS NOT NULL)
    ),
    CONSTRAINT ck_chat_runs_error_code CHECK (
        error_code IS NULL OR (state IN ('failed', 'cancelled') AND length(error_code) <= 64)
    ),
    CONSTRAINT fk_chat_runs_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_chat_runs_thread FOREIGN KEY (thread_id, org_id)
        REFERENCES chat_threads (id, org_id),
    CONSTRAINT fk_chat_runs_created_by FOREIGN KEY (created_by) REFERENCES users (id)
);
CREATE INDEX ix_chat_runs_thread_id_created_at ON chat_runs (thread_id, created_at);
CREATE INDEX ix_chat_runs_active_created_by ON chat_runs (org_id, created_by)
    WHERE state IN ('queued', 'running');
CREATE INDEX ix_chat_runs_running_lease ON chat_runs (lease_expires_at) WHERE state = 'running';
CREATE INDEX ix_chat_runs_created_by_created_at ON chat_runs (created_by, created_at);

CREATE TABLE chat_messages (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    citations jsonb NOT NULL DEFAULT '[]'::jsonb,
    verification jsonb,
    run_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_chat_messages PRIMARY KEY (id),
    CONSTRAINT uq_chat_messages_id_thread_id UNIQUE (id, thread_id),
    CONSTRAINT ck_chat_messages_role CHECK (role IN ('user', 'assistant')),
    CONSTRAINT ck_chat_messages_content CHECK (length(content) BETWEEN 1 AND 100000),
    CONSTRAINT ck_chat_messages_citations CHECK (jsonb_typeof(citations) = 'array'),
    CONSTRAINT fk_chat_messages_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_chat_messages_thread FOREIGN KEY (thread_id, org_id)
        REFERENCES chat_threads (id, org_id),
    CONSTRAINT fk_chat_messages_run FOREIGN KEY (run_id, thread_id)
        REFERENCES chat_runs (id, thread_id)
);
CREATE INDEX ix_chat_messages_thread_id_created_at ON chat_messages (thread_id, created_at, id);
CREATE INDEX ix_chat_messages_run_id ON chat_messages (run_id) WHERE run_id IS NOT NULL;

-- The run points at its user message and the message at its run: the run is inserted first (the
-- idempotency key decides who wins), so this FK is checked at COMMIT.
ALTER TABLE chat_runs ADD CONSTRAINT fk_chat_runs_user_message
    FOREIGN KEY (user_message_id, thread_id) REFERENCES chat_messages (id, thread_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE chat_run_events (
    seq bigint GENERATED ALWAYS AS IDENTITY,
    run_id uuid NOT NULL,
    org_id uuid NOT NULL,
    type text NOT NULL,
    data jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_chat_run_events PRIMARY KEY (seq),
    CONSTRAINT ck_chat_run_events_type CHECK (type IN {EVENT_TYPES}),
    CONSTRAINT ck_chat_run_events_data CHECK (octet_length(data::text) <= 65536),
    CONSTRAINT fk_chat_run_events_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_chat_run_events_run FOREIGN KEY (run_id, org_id)
        REFERENCES chat_runs (id, org_id)
);
CREATE INDEX ix_chat_run_events_run_id_seq ON chat_run_events (run_id, seq);
""".replace("{RUN_STATES}", RUN_STATES).replace("{EVENT_TYPES}", EVENT_TYPES)

TRIGGER_SQL = """
CREATE FUNCTION ukos_chat_messages_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'chat_messages is append-only (% rejected)', TG_OP
        USING ERRCODE = '42501', CONSTRAINT = 'chat_messages_append_only';
END;
$$;
REVOKE ALL ON FUNCTION ukos_chat_messages_append_only() FROM PUBLIC;
CREATE TRIGGER chat_messages_append_only
    BEFORE UPDATE OR DELETE ON chat_messages
    FOR EACH ROW EXECUTE FUNCTION ukos_chat_messages_append_only();
CREATE TRIGGER chat_messages_no_truncate
    BEFORE TRUNCATE ON chat_messages
    FOR EACH STATEMENT EXECUTE FUNCTION ukos_chat_messages_append_only();

CREATE FUNCTION ukos_chat_run_events_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'chat_run_events is append-only (% rejected)', TG_OP
        USING ERRCODE = '42501', CONSTRAINT = 'chat_run_events_append_only';
END;
$$;
REVOKE ALL ON FUNCTION ukos_chat_run_events_append_only() FROM PUBLIC;
CREATE TRIGGER chat_run_events_append_only
    BEFORE UPDATE OR DELETE ON chat_run_events
    FOR EACH ROW EXECUTE FUNCTION ukos_chat_run_events_append_only();
CREATE TRIGGER chat_run_events_no_truncate
    BEFORE TRUNCATE ON chat_run_events
    FOR EACH STATEMENT EXECUTE FUNCTION ukos_chat_run_events_append_only();
"""

_IS_ORG_ADMIN = (
    "EXISTS (SELECT 1 FROM organization_memberships m WHERE m.org_id = ai_policies.org_id"
    f" AND m.user_id = {CUR_USER} AND m.role IN ('owner', 'admin'))"
)
_THREAD_VISIBLE = (
    "EXISTS (SELECT 1 FROM chat_threads t WHERE t.id = {table}.thread_id"
    " AND t.org_id = {table}.org_id{extra})"
)
_RUN_VISIBLE = (
    "EXISTS (SELECT 1 FROM chat_runs r WHERE r.id = chat_run_events.run_id"
    " AND r.org_id = chat_run_events.org_id)"
)


def _thread_visible(table: str, *, live: bool = False) -> str:
    return _THREAD_VISIBLE.format(table=table, extra=" AND t.deleted_at IS NULL" if live else "")


# Per-command policies, only for the commands some role is granted. Subqueries run under the
# caller's own RLS: the thread policy (creator only) therefore propagates to messages and runs, and
# the run policy to events.
RLS_SQL = f"""
CREATE POLICY ai_policies_select ON ai_policies FOR SELECT USING (org_id = {CUR_ORG});
CREATE POLICY ai_policies_insert ON ai_policies FOR INSERT
    WITH CHECK (org_id = {CUR_ORG} AND {_IS_ORG_ADMIN});
CREATE POLICY ai_policies_update ON ai_policies FOR UPDATE
    USING (org_id = {CUR_ORG} AND {_IS_ORG_ADMIN})
    WITH CHECK (org_id = {CUR_ORG} AND {_IS_ORG_ADMIN});

CREATE POLICY chat_threads_select ON chat_threads FOR SELECT
    USING (org_id = {CUR_ORG} AND created_by = {CUR_USER});
CREATE POLICY chat_threads_insert ON chat_threads FOR INSERT
    WITH CHECK (org_id = {CUR_ORG} AND created_by = {CUR_USER});
CREATE POLICY chat_threads_update ON chat_threads FOR UPDATE
    USING (org_id = {CUR_ORG} AND created_by = {CUR_USER})
    WITH CHECK (org_id = {CUR_ORG} AND created_by = {CUR_USER});

CREATE POLICY chat_messages_select ON chat_messages FOR SELECT
    USING (org_id = {CUR_ORG} AND {_thread_visible("chat_messages")});
CREATE POLICY chat_messages_insert ON chat_messages FOR INSERT
    WITH CHECK (org_id = {CUR_ORG} AND {_thread_visible("chat_messages", live=True)});

CREATE POLICY chat_runs_select ON chat_runs FOR SELECT
    USING (org_id = {CUR_ORG} AND created_by = {CUR_USER});
CREATE POLICY chat_runs_insert ON chat_runs FOR INSERT
    WITH CHECK (org_id = {CUR_ORG} AND created_by = {CUR_USER}
                AND {_thread_visible("chat_runs", live=True)});
CREATE POLICY chat_runs_update ON chat_runs FOR UPDATE
    USING (org_id = {CUR_ORG} AND created_by = {CUR_USER})
    WITH CHECK (org_id = {CUR_ORG} AND created_by = {CUR_USER});

CREATE POLICY chat_run_events_select ON chat_run_events FOR SELECT
    USING (org_id = {CUR_ORG} AND {_RUN_VISIBLE});
CREATE POLICY chat_run_events_insert ON chat_run_events FOR INSERT
    WITH CHECK (org_id = {CUR_ORG} AND {_RUN_VISIBLE});
"""

FUNCTIONS_SQL = """
CREATE FUNCTION chat_claim_run(p_run_id uuid, p_owner text, p_lease_seconds integer)
RETURNS TABLE (
    run_id uuid, org_id uuid, thread_id uuid, user_message_id uuid, created_by uuid,
    attempts integer, lease_epoch bigint
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_run public.chat_runs%ROWTYPE;
BEGIN
    IF p_owner IS NULL OR length(p_owner) = 0 OR p_lease_seconds IS NULL OR p_lease_seconds < 1 THEN
        RAISE EXCEPTION 'chat_claim_run: owner and a positive lease are required'
            USING ERRCODE = '22023';
    END IF;
    -- Targeted by id and only while queued; a row somebody else holds locked is skipped, so a
    -- duplicate delivery (or a concurrent claim) claims nothing.
    SELECT * INTO v_run FROM public.chat_runs r
        WHERE r.id = p_run_id AND r.state = 'queued'
        FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    UPDATE public.chat_runs r SET
        state = 'running',
        lease_owner = p_owner,
        lease_expires_at = now() + make_interval(secs => p_lease_seconds),
        lease_epoch = v_run.lease_epoch + 1,
        attempts = v_run.attempts + 1,
        started_at = COALESCE(r.started_at, now())
    WHERE r.id = v_run.id;
    RETURN QUERY SELECT v_run.id, v_run.org_id, v_run.thread_id, v_run.user_message_id,
        v_run.created_by, v_run.attempts + 1, v_run.lease_epoch + 1;
END;
$$;

-- An expired lease means the worker died or stalled. A run that has not streamed a single token
-- yet (no ``delta`` event) is requeued while ``attempts < p_max_attempts`` (a client has seen
-- nothing to repeat); once tokens went out, or the attempts are used up, it ends ``failed`` with
-- ``lease_expired`` and a ``failed`` event so a connected stream terminates. Reconnecting never
-- regenerates a streamed answer.
CREATE FUNCTION chat_reap_expired_runs(p_max_attempts integer)
RETURNS TABLE (run_id uuid, org_id uuid, outcome text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_run public.chat_runs%ROWTYPE;
BEGIN
    IF p_max_attempts IS NULL OR p_max_attempts < 1 THEN
        RAISE EXCEPTION 'chat_reap_expired_runs: max attempts must be >= 1' USING ERRCODE = '22023';
    END IF;
    FOR v_run IN
        SELECT * FROM public.chat_runs r
        WHERE r.state = 'running' AND r.lease_expires_at < now()
        ORDER BY r.lease_expires_at
        FOR UPDATE SKIP LOCKED
    LOOP
        IF v_run.attempts < p_max_attempts AND NOT EXISTS (
            SELECT 1 FROM public.chat_run_events e WHERE e.run_id = v_run.id AND e.type = 'delta'
        ) THEN
            UPDATE public.chat_runs r SET
                state = 'queued', lease_owner = NULL, lease_expires_at = NULL
            WHERE r.id = v_run.id;
            INSERT INTO public.outbox_events
                (org_id, aggregate_type, aggregate_id, event_type, payload, available_at)
            VALUES (v_run.org_id, 'chat_run', v_run.id, 'chat.run.queued',
                    jsonb_build_object('run_id', v_run.id), now());
            RETURN QUERY SELECT v_run.id, v_run.org_id, 'requeued'::text;
        ELSE
            UPDATE public.chat_runs r SET
                state = 'failed', lease_owner = NULL, lease_expires_at = NULL,
                error_code = 'lease_expired', finished_at = now()
            WHERE r.id = v_run.id;
            INSERT INTO public.chat_run_events (run_id, org_id, type, data)
            VALUES (v_run.id, v_run.org_id, 'failed',
                    jsonb_build_object('error_code', 'lease_expired'));
            RETURN QUERY SELECT v_run.id, v_run.org_id, 'failed'::text;
        END IF;
    END LOOP;
END;
$$;

-- The API role may not UPDATE runs, so "cancel" is this one narrow transition. It acts only on the
-- caller's own run in the caller's org (``app.current_org_id`` + ``app.current_user_id``, set by
-- the tenant transaction); anything else returns no row (the API answers a concealed 404). The
-- state change fences the worker out: its event/state writes require ``state = 'running'``.
CREATE FUNCTION chat_cancel_run(p_run_id uuid)
RETURNS TABLE (run_id uuid, state text, cancelled boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
DECLARE
    v_org uuid := nullif(current_setting('app.current_org_id', true), '')::uuid;
    v_user uuid := nullif(current_setting('app.current_user_id', true), '')::uuid;
    v_run public.chat_runs%ROWTYPE;
BEGIN
    IF v_org IS NULL OR v_user IS NULL OR p_run_id IS NULL THEN
        RETURN;
    END IF;
    SELECT * INTO v_run FROM public.chat_runs r
        WHERE r.id = p_run_id AND r.org_id = v_org AND r.created_by = v_user
        FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF v_run.state IN ('queued', 'running') THEN
        UPDATE public.chat_runs r SET
            state = 'cancelled', lease_owner = NULL, lease_expires_at = NULL,
            finished_at = now()
        WHERE r.id = v_run.id;
        INSERT INTO public.chat_run_events (run_id, org_id, type, data)
        VALUES (v_run.id, v_run.org_id, 'cancelled', jsonb_build_object('reason', 'user'));
        RETURN QUERY SELECT v_run.id, 'cancelled'::text, true;
    ELSE
        RETURN QUERY SELECT v_run.id, v_run.state, false;
    END IF;
END;
$$;

-- Contract m04 §2: org owner/admin -> every live workspace; otherwise the live workspaces with an
-- explicit membership; nothing unless the user is a member of a live org.
CREATE FUNCTION chat_readable_workspaces(p_org_id uuid, p_user_id uuid)
RETURNS TABLE (workspace_id uuid)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
BEGIN
    IF p_org_id IS NULL OR p_user_id IS NULL THEN
        RETURN;
    END IF;
    RETURN QUERY
        SELECT w.id
        FROM public.workspaces w
        JOIN public.organizations o ON o.id = w.org_id AND o.deleted_at IS NULL
        JOIN public.organization_memberships m ON m.org_id = w.org_id AND m.user_id = p_user_id
        WHERE w.org_id = p_org_id AND w.deleted_at IS NULL
          AND (m.role IN ('owner', 'admin')
               OR EXISTS (SELECT 1 FROM public.workspace_memberships wm
                          WHERE wm.workspace_id = w.id AND wm.user_id = p_user_id))
        ORDER BY w.id;
END;
$$;
"""


def _app_grants_sql(role: str) -> str:
    return f"""
GRANT SELECT ON ai_policies, chat_run_events TO "{role}";
GRANT INSERT ON ai_policies TO "{role}";
GRANT UPDATE (allow_external, allowed_providers, updated_by, updated_at)
    ON ai_policies TO "{role}";
GRANT SELECT, INSERT ON chat_threads, chat_messages, chat_runs TO "{role}";
-- Soft delete and rename only; the creator is fixed at insert.
GRANT UPDATE (title, deleted_at, updated_at) ON chat_threads TO "{role}";
"""


def _worker_grants_sql(role: str) -> str:
    return f"""
GRANT SELECT ON ai_policies, chat_threads, chat_messages, chat_runs, chat_run_events TO "{role}";
GRANT INSERT ON chat_messages, chat_run_events TO "{role}";
-- State machine columns only; identity, ownership, idempotency key, scope, attempts and the fencing
-- epoch change at creation, through the definer functions, or never. ``model`` is written once by
-- the runtime (provider/model id/prompt version), hence part of the grant.
GRANT UPDATE (state, model, started_at, finished_at, error_code, lease_owner, lease_expires_at)
    ON chat_runs TO "{role}";
-- A finished run bumps its thread's recency.
GRANT UPDATE (updated_at) ON chat_threads TO "{role}";
"""


def upgrade() -> None:
    worker = _worker_role()
    op.execute(SCHEMA_SQL)
    op.execute(TRIGGER_SQL)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(RLS_SQL)
    op.execute("ALTER TABLE outbox_events DROP CONSTRAINT ck_outbox_events_type")
    op.execute(
        "ALTER TABLE outbox_events ADD CONSTRAINT ck_outbox_events_type"
        f" CHECK (event_type IN ({OUTBOX_TYPES_AFTER}))"
    )
    op.execute(FUNCTIONS_SQL)
    for signature in FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(_app_grants_sql(_app_role()))
    op.execute(_worker_grants_sql(worker))
    for signature in FUNCTIONS:
        grantee = _app_role() if signature.startswith("chat_cancel_run") else worker
        op.execute(f'GRANT EXECUTE ON FUNCTION {signature} TO "{grantee}"')


def downgrade() -> None:
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    # Referencing tables first. chat_runs and chat_messages reference each other (the run -> user
    # message FK is deferrable); dropping the constraint breaks the cycle.
    op.execute("ALTER TABLE chat_runs DROP CONSTRAINT IF EXISTS fk_chat_runs_user_message")
    for table in ("chat_run_events", "chat_messages", "chat_runs", "chat_threads", "ai_policies"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP FUNCTION IF EXISTS ukos_chat_messages_append_only()")
    op.execute("DROP FUNCTION IF EXISTS ukos_chat_run_events_append_only()")
    # A row of the type this revision introduced would violate the old CHECK.
    op.execute("DELETE FROM outbox_events WHERE event_type = 'chat.run.queued'")
    op.execute("ALTER TABLE outbox_events DROP CONSTRAINT ck_outbox_events_type")
    op.execute(
        "ALTER TABLE outbox_events ADD CONSTRAINT ck_outbox_events_type"
        f" CHECK (event_type IN ({OUTBOX_TYPES_BEFORE}))"
    )
