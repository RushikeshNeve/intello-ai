"""M12.05 re-embedding / index migration: ``reindex_jobs``, ``org_active_spaces`` and the atomic
cutover / rollback functions.

* ``reindex_jobs``: one blue/green re-index of an org from one embedding space to another. State
  ``planned | running | verifying | ready | cutover | rolled_back | failed``; counts; a lease
  (``lease_owner``, ``lease_expires_at``, ``lease_epoch`` fencing token) like ``ingestion_jobs``.
  At most one non-terminal job per org. FORCE RLS by org.
* ``org_active_spaces``: the **active-space pointer** per org (PostgreSQL is the authority). Absent
  row = the configured default space.
* ``reindex_cutover(job)`` / ``reindex_rollback(job)`` (definer, app role, org owner/admin only):
  swap every document's publication pointer between the old and the new space's revisions, flip
  ``org_active_spaces`` and the job state in ONE transaction, and queue a ``knowledge.reconcile``
  outbox event per document (the existing reconcile aligns the RushDB ``published`` flags).

Revision ID: 0018_reindex
Revises: 0011_member_preapprovals
Create Date: 2026-11-21
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0018_reindex"
down_revision: str | None = "0011_member_preapprovals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
CUR_USER = "nullif(current_setting('app.current_user_id', true), '')::uuid"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
FUNCTIONS = ("reindex_cutover(uuid)", "reindex_rollback(uuid)")


def _role(key: str, env: str, default: str) -> str:
    value = (
        context.config.attributes.get(key)
        or context.get_x_argument(as_dictionary=True).get(key)
        or os.environ.get(env)
        or default
    )
    if not ROLE_PATTERN.fullmatch(value):
        raise ValueError(f"invalid {key} name")
    return str(value)


SCHEMA_SQL = """
CREATE TABLE reindex_jobs (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    from_space text NOT NULL,
    to_space text NOT NULL,
    to_dimensions integer NOT NULL,
    state text NOT NULL DEFAULT 'planned',
    total integer NOT NULL DEFAULT 0,
    done integer NOT NULL DEFAULT 0,
    failed integer NOT NULL DEFAULT 0,
    requested_by uuid,
    lease_owner text,
    lease_expires_at timestamptz,
    lease_epoch integer NOT NULL DEFAULT 0,
    last_error_code text,
    old_space_purged_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    CONSTRAINT pk_reindex_jobs PRIMARY KEY (id),
    CONSTRAINT fk_reindex_jobs_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT ck_reindex_jobs_state CHECK (state IN
        ('planned', 'running', 'verifying', 'ready', 'cutover', 'rolled_back', 'failed')),
    CONSTRAINT ck_reindex_jobs_spaces CHECK (from_space <> to_space),
    CONSTRAINT ck_reindex_jobs_counts CHECK (total >= 0 AND done >= 0 AND failed >= 0),
    CONSTRAINT ck_reindex_jobs_dimensions CHECK (to_dimensions >= 1)
);
CREATE UNIQUE INDEX uq_reindex_jobs_one_active
    ON reindex_jobs (org_id) WHERE state IN ('planned', 'running', 'verifying', 'ready');
CREATE INDEX ix_reindex_jobs_org_created ON reindex_jobs (org_id, created_at DESC);

CREATE TABLE org_active_spaces (
    org_id uuid NOT NULL,
    space_id text NOT NULL,
    reindex_job_id uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_org_active_spaces PRIMARY KEY (org_id),
    CONSTRAINT fk_org_active_spaces_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE
);
"""

# Shared by cutover and rollback: lock every document's publication, then pair each pointer in
# the ``src`` space with its ``ready`` revision in the ``dst`` space (new_rev NULL = no mirror).
_SWAP = """
    FOR d IN
        SELECT p.document_id FROM public.document_publications p
        WHERE p.org_id = j.org_id ORDER BY p.document_id
    LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended('doc-publication:' || d.document_id, 0));
    END LOOP;
    CREATE TEMP TABLE _reindex_swap ON COMMIT DROP AS
        SELECT p.document_id, p.revision_id AS old_rev, r1.id AS new_rev
        FROM public.document_publications p
        JOIN public.knowledge_revisions r0 ON r0.id = p.revision_id
        LEFT JOIN public.knowledge_revisions r1
               ON r1.version_id = p.version_id AND r1.pipeline_version = r0.pipeline_version
              AND r1.embedding_space_id = {dst} AND r1.state = 'ready'
        WHERE p.org_id = j.org_id AND r0.embedding_space_id = {src};
"""

_APPLY = """
    UPDATE public.knowledge_revisions SET state = 'ready'
        WHERE id IN (SELECT old_rev FROM _reindex_swap);
    UPDATE public.knowledge_revisions SET state = 'published', published_at = now()
        WHERE id IN (SELECT new_rev FROM _reindex_swap);
    UPDATE public.document_publications p
        SET revision_id = s.new_rev, previous_revision_id = s.old_rev, published_at = now(),
            reconciled_at = NULL
        FROM _reindex_swap s WHERE p.document_id = s.document_id;
    INSERT INTO public.outbox_events (org_id, aggregate_type, aggregate_id, event_type, payload)
        SELECT j.org_id, 'document', s.document_id, 'knowledge.reconcile',
               jsonb_build_object('document_id', s.document_id) FROM _reindex_swap s;
"""

_ADMIN_CHECK = f"""
    SELECT * INTO j FROM public.reindex_jobs WHERE id = p_job AND org_id = {CUR_ORG} FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM public.organization_memberships m
        WHERE m.org_id = j.org_id AND m.user_id = {CUR_USER} AND m.role IN ('owner', 'admin')
    ) THEN
        RETURN 'not_found';
    END IF;
"""

FUNCTIONS_SQL = f"""
CREATE FUNCTION reindex_cutover(p_job uuid) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    j public.reindex_jobs;
    d record;
    missing integer;
BEGIN
{_ADMIN_CHECK}
    IF j.state <> 'ready' THEN
        RETURN 'bad_state';
    END IF;
{_SWAP.format(src="j.from_space", dst="j.to_space")}
    SELECT count(*) INTO missing FROM _reindex_swap WHERE new_rev IS NULL;
    IF missing > 0 THEN
        RETURN 'incomplete';
    END IF;
{_APPLY}
    INSERT INTO public.org_active_spaces (org_id, space_id, reindex_job_id)
        VALUES (j.org_id, j.to_space, j.id)
        ON CONFLICT (org_id) DO UPDATE
        SET space_id = excluded.space_id, reindex_job_id = excluded.reindex_job_id,
            updated_at = now();
    UPDATE public.reindex_jobs SET state = 'cutover', finished_at = now(), updated_at = now(),
        lease_owner = NULL, lease_expires_at = NULL WHERE id = j.id;
    RETURN 'ok';
END $$;

CREATE FUNCTION reindex_rollback(p_job uuid) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    j public.reindex_jobs;
    d record;
BEGIN
{_ADMIN_CHECK}
    IF j.state <> 'cutover' OR j.old_space_purged_at IS NOT NULL THEN
        RETURN 'bad_state';
    END IF;
{_SWAP.format(src="j.to_space", dst="j.from_space")}
    -- Documents first published after the cutover have no revision in the old space: they stay
    -- where they are (keyword search still finds them); only mirrored documents flip back.
    DELETE FROM _reindex_swap WHERE new_rev IS NULL;
{_APPLY}
    INSERT INTO public.org_active_spaces (org_id, space_id, reindex_job_id)
        VALUES (j.org_id, j.from_space, j.id)
        ON CONFLICT (org_id) DO UPDATE
        SET space_id = excluded.space_id, reindex_job_id = excluded.reindex_job_id,
            updated_at = now();
    UPDATE public.reindex_jobs SET state = 'rolled_back', updated_at = now() WHERE id = j.id;
    RETURN 'ok';
END $$;
"""

RLS_SQL = f"""
CREATE POLICY reindex_jobs_select ON reindex_jobs FOR SELECT USING (org_id = {CUR_ORG});
CREATE POLICY reindex_jobs_insert ON reindex_jobs FOR INSERT
    WITH CHECK (org_id = {CUR_ORG} AND state = 'planned');
CREATE POLICY reindex_jobs_update ON reindex_jobs FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY org_active_spaces_select ON org_active_spaces FOR SELECT USING (org_id = {CUR_ORG});
"""


def upgrade() -> None:
    app = _role("app_role", "POSTGRES_APP_USER", "ukos_app")
    worker = _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")
    op.execute(SCHEMA_SQL)
    for table in ("reindex_jobs", "org_active_spaces"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(RLS_SQL)
    op.execute(FUNCTIONS_SQL)
    for signature in FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f'GRANT EXECUTE ON FUNCTION {signature} TO "{app}"')
    op.execute(f'GRANT SELECT, INSERT ON reindex_jobs TO "{app}"')
    op.execute(f'GRANT SELECT ON org_active_spaces TO "{app}", "{worker}"')
    op.execute(f'GRANT SELECT ON reindex_jobs TO "{worker}"')
    op.execute(
        "GRANT UPDATE (state, total, done, failed, lease_owner, lease_expires_at, lease_epoch,"
        " last_error_code, old_space_purged_at, updated_at, started_at, finished_at)"
        f' ON reindex_jobs TO "{worker}"'
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS org_active_spaces")
    op.execute("DROP TABLE IF EXISTS reindex_jobs")
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
