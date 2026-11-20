"""Connectors (M06.01): accounts, sync runs, item map, connector columns on ``sources``.

Contract: docs/architecture/m06-contracts.md section 2.

* ``connector_accounts``: the (optional, AES-256-GCM sealed) token of a provider account. A revoked
  account has ``token_ciphertext`` NULL (wiped).
* ``source_syncs``: one row per requested sync. Partial unique indexes allow at most ONE ``running``
  and ONE ``queued`` sync per source (so a schedule tick, a webhook burst and a manual request never
  pile up) and one row per webhook ``delivery_id`` (a duplicate delivery inserts nothing).
* ``source_items``: remote item -> document map, unique ``(source_id, remote_id)``.
* ``sources`` gets the connector columns (cursor, interval, next_sync_at, state, sealed webhook
  secret) and ``uq_sources_id_org_id`` for the composite FKs. The webhook secret is stored SEALED
  (not hashed as the contract sketched): verifying ``X-Hub-Signature-256`` needs the secret itself.
* RLS ENABLE + FORCE, per-command policies on ``app.current_org_id`` (no ``TO`` clause).
* Grants (least privilege). API role: SELECT/INSERT on accounts and syncs, SELECT on items; column
  UPDATEs for revoke (``sources.state``, ``connector_accounts.token_ciphertext``, ...). Worker role
  (the sync task runs in the worker under ``app.current_org_id``): SELECT on the four tables,
  INSERT/UPDATE on syncs and items, INSERT on ``documents``/``document_versions`` plus column UPDATE
  on ``documents`` (version pointer, logical deletion) and on ``sources`` (cursor, next_sync_at,
  state). It never gets DELETE anywhere.
* SECURITY DEFINER functions: ``connector_enqueue_due_syncs(limit)`` (worker; the dispatcher's
  scheduler: bumps ``next_sync_at``, inserts the queued sync and its ``source.sync.requested``
  outbox row in one statement batch, and fails syncs stuck in ``running``) and
  ``connector_webhook_lookup(source_id)`` (API; the unauthenticated webhook must find the org and
  the sealed secret of a source id before any org context exists).
* The outbox CHECK is extended with ``source.sync.requested``.

Revision ID: 0009_connectors
Revises: 0008_graph
Create Date: 2026-11-19
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0009_connectors"
down_revision: str | None = "0008_graph"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

TABLES = ("connector_accounts", "source_syncs", "source_items")
FUNCTIONS = (
    "connector_enqueue_due_syncs(integer)",
    "connector_webhook_lookup(uuid)",
)

OUTBOX_TYPES_BEFORE = (
    "'ingestion.job.queued', 'knowledge.revision.published', 'document.deleted', "
    "'knowledge.reconcile', 'chat.run.queued'"
)
OUTBOX_TYPES_AFTER = OUTBOX_TYPES_BEFORE + ", 'source.sync.requested'"


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
ALTER TABLE sources ADD CONSTRAINT uq_sources_id_org_id UNIQUE (id, org_id);

CREATE TABLE connector_accounts (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    provider text NOT NULL,
    label text NOT NULL,
    token_ciphertext bytea,
    key_id text,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    CONSTRAINT pk_connector_accounts PRIMARY KEY (id),
    CONSTRAINT uq_connector_accounts_id_org_id UNIQUE (id, org_id),
    CONSTRAINT ck_connector_accounts_provider CHECK (provider IN ('github')),
    CONSTRAINT ck_connector_accounts_label CHECK (length(label) BETWEEN 1 AND 255),
    CONSTRAINT ck_connector_accounts_key_id CHECK ((token_ciphertext IS NULL) = (key_id IS NULL)),
    CONSTRAINT fk_connector_accounts_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_connector_accounts_created_by FOREIGN KEY (created_by) REFERENCES users (id)
);

ALTER TABLE sources
    ADD COLUMN connector_account_id uuid,
    ADD COLUMN sync_cursor text,
    ADD COLUMN sync_interval_seconds integer,
    ADD COLUMN next_sync_at timestamptz,
    ADD COLUMN state text NOT NULL DEFAULT 'active',
    ADD COLUMN revoked_at timestamptz,
    ADD COLUMN webhook_secret_ciphertext bytea,
    ADD COLUMN webhook_key_id text,
    ADD CONSTRAINT ck_sources_state CHECK (state IN ('active', 'revoked', 'error')),
    ADD CONSTRAINT ck_sources_sync_interval CHECK (
        sync_interval_seconds IS NULL OR sync_interval_seconds BETWEEN 300 AND 2592000),
    ADD CONSTRAINT ck_sources_connector_account CHECK (
        kind <> 'connector' OR connector_account_id IS NOT NULL),
    ADD CONSTRAINT fk_sources_connector_account FOREIGN KEY (connector_account_id, org_id)
        REFERENCES connector_accounts (id, org_id);
CREATE INDEX ix_sources_next_sync_at ON sources (next_sync_at)
    WHERE kind = 'connector' AND state = 'active' AND next_sync_at IS NOT NULL;

CREATE TABLE source_syncs (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    source_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'queued',
    trigger text NOT NULL,
    delivery_id text,
    cursor_before text,
    cursor_after text,
    stats jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    CONSTRAINT pk_source_syncs PRIMARY KEY (id),
    CONSTRAINT ck_source_syncs_state CHECK (
        state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    CONSTRAINT ck_source_syncs_trigger CHECK (trigger IN ('schedule', 'manual', 'webhook')),
    CONSTRAINT ck_source_syncs_delivery_id CHECK (length(delivery_id) <= 128),
    CONSTRAINT ck_source_syncs_error_code CHECK (length(error_code) <= 64),
    CONSTRAINT fk_source_syncs_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_source_syncs_source FOREIGN KEY (source_id, org_id)
        REFERENCES sources (id, org_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX uq_source_syncs_one_running ON source_syncs (source_id) WHERE state = 'running';
CREATE UNIQUE INDEX uq_source_syncs_one_queued ON source_syncs (source_id) WHERE state = 'queued';
CREATE UNIQUE INDEX uq_source_syncs_delivery ON source_syncs (source_id, delivery_id)
    WHERE delivery_id IS NOT NULL;
CREATE INDEX ix_source_syncs_source_id_created_at ON source_syncs (source_id, created_at);

CREATE TABLE source_items (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    source_id uuid NOT NULL,
    remote_id text NOT NULL,
    path text NOT NULL,
    document_id uuid,
    version_token text,
    content_sha256 text,
    state text NOT NULL DEFAULT 'active',
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_source_items PRIMARY KEY (id),
    CONSTRAINT uq_source_items_source_id_remote_id UNIQUE (source_id, remote_id),
    CONSTRAINT ck_source_items_state CHECK (state IN ('active', 'deleted')),
    CONSTRAINT ck_source_items_sha256 CHECK (
        content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT fk_source_items_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_source_items_source FOREIGN KEY (source_id, org_id)
        REFERENCES sources (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_source_items_document_id FOREIGN KEY (document_id) REFERENCES documents (id)
);
CREATE INDEX ix_source_items_document_id ON source_items (document_id);
"""

RLS_SQL = f"""
CREATE POLICY connector_accounts_select ON connector_accounts FOR SELECT
    USING (org_id = {CUR_ORG});
CREATE POLICY connector_accounts_insert ON connector_accounts FOR INSERT
    WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY connector_accounts_update ON connector_accounts FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});

CREATE POLICY sources_update ON sources FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});

CREATE POLICY source_syncs_select ON source_syncs FOR SELECT USING (org_id = {CUR_ORG});
CREATE POLICY source_syncs_insert ON source_syncs FOR INSERT WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY source_syncs_update ON source_syncs FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});

CREATE POLICY source_items_select ON source_items FOR SELECT USING (org_id = {CUR_ORG});
CREATE POLICY source_items_insert ON source_items FOR INSERT WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY source_items_update ON source_items FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});
"""

FUNCTIONS_SQL = """
-- The scheduler. One call = one tick: sources whose next_sync_at passed get a queued sync and an
-- outbox row, and their next_sync_at moves one interval ahead; syncs stuck in 'running' (a worker
-- died) are failed so the source is not blocked forever. Idempotent: the partial unique index on
-- queued syncs means a source with a queued sync gets nothing new.
CREATE FUNCTION connector_enqueue_due_syncs(p_limit integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_src record;
    v_sync uuid;
    v_count integer := 0;
BEGIN
    IF p_limit IS NULL OR p_limit < 1 THEN
        RAISE EXCEPTION 'connector_enqueue_due_syncs: limit must be >= 1' USING ERRCODE = '22023';
    END IF;
    UPDATE public.source_syncs SET state = 'failed', error_code = 'stale', finished_at = now()
        WHERE state = 'running' AND started_at < now() - interval '30 minutes';
    FOR v_src IN
        SELECT s.id, s.org_id, s.sync_interval_seconds FROM public.sources s
        WHERE s.kind = 'connector' AND s.state = 'active'
          AND s.next_sync_at IS NOT NULL AND s.next_sync_at <= now()
        ORDER BY s.next_sync_at
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    LOOP
        UPDATE public.sources s SET
            next_sync_at = now()
                + make_interval(secs => coalesce(v_src.sync_interval_seconds, 3600))
        WHERE s.id = v_src.id;
        v_sync := gen_random_uuid();
        INSERT INTO public.source_syncs (id, org_id, source_id, state, trigger)
            VALUES (v_sync, v_src.org_id, v_src.id, 'queued', 'schedule')
            ON CONFLICT DO NOTHING;
        IF FOUND THEN
            INSERT INTO public.outbox_events
                (org_id, aggregate_type, aggregate_id, event_type, payload, available_at)
            VALUES (v_src.org_id, 'source', v_src.id, 'source.sync.requested',
                    jsonb_build_object('source_id', v_src.id, 'sync_id', v_sync), now());
            v_count := v_count + 1;
        END IF;
    END LOOP;
    RETURN v_count;
END;
$$;

-- Webhook entry: the request has no org context, so the sealed secret is looked up by source id.
CREATE FUNCTION connector_webhook_lookup(p_source_id uuid)
RETURNS TABLE (org_id uuid, state text, secret bytea, key_id text)
LANGUAGE sql SECURITY DEFINER STABLE SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT s.org_id, s.state, s.webhook_secret_ciphertext, s.webhook_key_id
    FROM public.sources s WHERE s.id = p_source_id AND s.kind = 'connector';
$$;
"""


def _app_grants_sql(role: str) -> str:
    return f"""
GRANT SELECT, INSERT ON connector_accounts, source_syncs TO "{role}";
GRANT SELECT ON source_items TO "{role}";
GRANT UPDATE (token_ciphertext, key_id, revoked_at) ON connector_accounts TO "{role}";
GRANT UPDATE (state, error_code, finished_at) ON source_syncs TO "{role}";
GRANT UPDATE (state) ON source_items TO "{role}";
GRANT UPDATE (state, revoked_at, next_sync_at, sync_interval_seconds, webhook_secret_ciphertext,
              webhook_key_id) ON sources TO "{role}";
"""


def _worker_grants_sql(role: str) -> str:
    return f"""
GRANT SELECT ON sources, connector_accounts TO "{role}";
GRANT UPDATE (sync_cursor, next_sync_at, state) ON sources TO "{role}";
GRANT SELECT ON source_syncs TO "{role}";
GRANT UPDATE (state, cursor_before, cursor_after, stats, error_code, started_at, finished_at)
    ON source_syncs TO "{role}";
GRANT SELECT, INSERT ON source_items TO "{role}";
GRANT UPDATE (path, document_id, version_token, content_sha256, state, last_seen_at)
    ON source_items TO "{role}";
GRANT INSERT ON documents, document_versions TO "{role}";
GRANT UPDATE (latest_version_id, updated_at, deleted_at, deleted_by) ON documents TO "{role}";
"""


def upgrade() -> None:
    app, worker = _app_role(), _worker_role()
    op.execute(SCHEMA_SQL)
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
    op.execute(_app_grants_sql(app))
    op.execute(_worker_grants_sql(worker))
    op.execute(f'GRANT EXECUTE ON FUNCTION connector_enqueue_due_syncs(integer) TO "{worker}"')
    op.execute(f'GRANT EXECUTE ON FUNCTION connector_webhook_lookup(uuid) TO "{app}"')


def downgrade() -> None:
    worker = _worker_role()
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    for table in ("source_items", "source_syncs"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP POLICY IF EXISTS sources_update ON sources")
    op.execute("DROP INDEX IF EXISTS ix_sources_next_sync_at")
    op.execute(
        "ALTER TABLE sources DROP CONSTRAINT IF EXISTS fk_sources_connector_account,"
        " DROP CONSTRAINT IF EXISTS ck_sources_connector_account,"
        " DROP CONSTRAINT IF EXISTS ck_sources_sync_interval,"
        " DROP CONSTRAINT IF EXISTS ck_sources_state,"
        " DROP COLUMN IF EXISTS connector_account_id, DROP COLUMN IF EXISTS sync_cursor,"
        " DROP COLUMN IF EXISTS sync_interval_seconds, DROP COLUMN IF EXISTS next_sync_at,"
        " DROP COLUMN IF EXISTS state, DROP COLUMN IF EXISTS revoked_at,"
        " DROP COLUMN IF EXISTS webhook_secret_ciphertext, DROP COLUMN IF EXISTS webhook_key_id,"
        " DROP CONSTRAINT IF EXISTS uq_sources_id_org_id"
    )
    op.execute("DROP TABLE IF EXISTS connector_accounts")
    op.execute("DELETE FROM outbox_events WHERE event_type = 'source.sync.requested'")
    op.execute("ALTER TABLE outbox_events DROP CONSTRAINT ck_outbox_events_type")
    op.execute(
        "ALTER TABLE outbox_events ADD CONSTRAINT ck_outbox_events_type"
        f" CHECK (event_type IN ({OUTBOX_TYPES_BEFORE}))"
    )
    # The worker's extra privileges on pre-existing tables go with this revision.
    exists = (
        op.get_bind().exec_driver_sql(f"SELECT 1 FROM pg_roles WHERE rolname = '{worker}'").scalar()
    )
    if exists:
        op.execute(f'REVOKE SELECT ON sources FROM "{worker}"')
        op.execute(f'REVOKE INSERT ON documents, document_versions FROM "{worker}"')
        op.execute(
            f"REVOKE UPDATE (latest_version_id, updated_at, deleted_at, deleted_by)"
            f' ON documents FROM "{worker}"'
        )
