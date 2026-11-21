"""Versioned prompt registry (M12.01).

* ``prompt_versions``: editable instruction bodies per ``(org, kind)``; ``org_id`` NULL = the global
  default (a copy of the repo default file). FORCE RLS: a row is visible to the context org when it
  is that org's or global (and only with an org context). The app role INSERTs ``draft`` rows of
  its own org only and UPDATEs ``state`` / ``activated_at`` of its own org's rows;
  global rows are written only by the definer.
* ``prompt_seed_default(kind, body, sha256)`` (definer): makes the global default of ``kind`` equal
  to the repo file (a new global version when the hash changed) and returns its id.
* The worker role reads (SELECT) and may call the seeder, so workers resolve prompts too.

Revision ID: 0016_prompt_versions
Revises: 0011_member_preapprovals
Create Date: 2026-11-21
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0016_prompt_versions"
down_revision: str | None = "0011_member_preapprovals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
CUR_USER = "nullif(current_setting('app.current_user_id', true), '')::uuid"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
KINDS = ("chat_system", "graph_extract", "research_plan")
SEED_FN = "prompt_seed_default(text, text, text)"


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


KINDS_SQL = ", ".join(f"'{k}'" for k in KINDS)

SCHEMA_SQL = f"""
CREATE TABLE prompt_versions (
    id uuid NOT NULL,
    org_id uuid,
    kind text NOT NULL,
    version integer NOT NULL,
    body text NOT NULL,
    sha256 text NOT NULL,
    change_note text,
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    state text NOT NULL DEFAULT 'draft',
    activated_at timestamptz,
    CONSTRAINT pk_prompt_versions PRIMARY KEY (id),
    CONSTRAINT ck_prompt_versions_kind CHECK (kind IN ({KINDS_SQL})),
    CONSTRAINT ck_prompt_versions_state CHECK (state IN ('draft', 'active', 'retired')),
    CONSTRAINT ck_prompt_versions_version CHECK (version >= 1),
    CONSTRAINT ck_prompt_versions_body CHECK (length(body) BETWEEN 1 AND 6000),
    CONSTRAINT ck_prompt_versions_sha256 CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
    CONSTRAINT fk_prompt_versions_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_prompt_versions_created_by FOREIGN KEY (created_by) REFERENCES users (id)
);
CREATE UNIQUE INDEX uq_prompt_versions_org_kind_version
    ON prompt_versions (org_id, kind, version) WHERE org_id IS NOT NULL;
CREATE UNIQUE INDEX uq_prompt_versions_global_kind_version
    ON prompt_versions (kind, version) WHERE org_id IS NULL;
CREATE UNIQUE INDEX uq_prompt_versions_org_active
    ON prompt_versions (org_id, kind) WHERE state = 'active' AND org_id IS NOT NULL;
CREATE UNIQUE INDEX uq_prompt_versions_global_active
    ON prompt_versions (kind) WHERE state = 'active' AND org_id IS NULL;
"""

RLS_SQL = f"""
CREATE POLICY prompt_versions_select ON prompt_versions FOR SELECT
    USING (org_id = {CUR_ORG} OR (org_id IS NULL AND {CUR_ORG} IS NOT NULL));
CREATE POLICY prompt_versions_insert ON prompt_versions FOR INSERT
    WITH CHECK (org_id = {CUR_ORG} AND created_by = {CUR_USER} AND state = 'draft');
CREATE POLICY prompt_versions_update ON prompt_versions FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});
"""

FUNCTION_SQL = """
CREATE FUNCTION prompt_seed_default(p_kind text, p_body text, p_sha text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    cur record;
    new_id uuid := gen_random_uuid();
BEGIN
    SELECT v.id, v.sha256 INTO cur FROM public.prompt_versions v
    WHERE v.org_id IS NULL AND v.kind = p_kind AND v.state = 'active';
    IF FOUND AND cur.sha256 = p_sha THEN
        RETURN cur.id;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('prompt_seed:' || p_kind));
    SELECT v.id, v.sha256 INTO cur FROM public.prompt_versions v
    WHERE v.org_id IS NULL AND v.kind = p_kind AND v.state = 'active';
    IF FOUND AND cur.sha256 = p_sha THEN
        RETURN cur.id;
    END IF;
    UPDATE public.prompt_versions SET state = 'retired'
    WHERE org_id IS NULL AND kind = p_kind AND state = 'active';
    INSERT INTO public.prompt_versions (id, org_id, kind, version, body, sha256, change_note,
                                        state, activated_at)
    VALUES (new_id, NULL, p_kind,
            COALESCE((SELECT max(version) FROM public.prompt_versions
                      WHERE org_id IS NULL AND kind = p_kind), 0) + 1,
            p_body, p_sha, 'repo default', 'active', now());
    RETURN new_id;
END $$;
"""


def upgrade() -> None:
    app = _role("app_role", "POSTGRES_APP_USER", "ukos_app")
    worker = _role("worker_role", "POSTGRES_WORKER_USER", "ukos_worker")
    op.execute(SCHEMA_SQL)
    op.execute("ALTER TABLE prompt_versions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE prompt_versions FORCE ROW LEVEL SECURITY")
    op.execute(RLS_SQL)
    op.execute(FUNCTION_SQL)
    op.execute(f"REVOKE ALL ON FUNCTION {SEED_FN} FROM PUBLIC")
    op.execute(f'GRANT EXECUTE ON FUNCTION {SEED_FN} TO "{app}", "{worker}"')
    op.execute(f'GRANT SELECT, INSERT ON prompt_versions TO "{app}"')
    op.execute(f'GRANT UPDATE (state, activated_at) ON prompt_versions TO "{app}"')
    op.execute(f'GRANT SELECT ON prompt_versions TO "{worker}"')


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SEED_FN}")
    op.execute("DROP TABLE IF EXISTS prompt_versions")
