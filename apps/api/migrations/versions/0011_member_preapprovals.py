"""Pre-approve a member by email (org members who have never signed in).

* ``users.email_verified``: set from the OIDC ``email_verified`` claim on every provisioning
  upsert. Pre-approvals and the "existing user" shortcut only ever match a VERIFIED email.
* ``org_member_preapprovals``: per org, FORCE RLS by org. The API role may SELECT / INSERT /
  DELETE rows of the context org only; a row is never updated by the API (consumption is done by
  the definer below). Unique per (org, lower(email)) while pending.
* ``preapproval_find_user(email)`` (definer, app): the single verified user with that email, or
  NULL; answers only for an owner/admin of the context org (``users`` RLS hides other orgs' users).
* ``preapproval_consume(user_id)`` (definer, app): called by the sign-in provisioner. For the
  context user (must equal ``user_id``) with a verified email, turns every pending pre-approval of
  a live org into an ``organization_memberships`` row (existing membership wins), stamps
  ``consumed_at`` and writes an ``org.member.added`` audit event (``via: preapproval``).

Revision ID: 0011_member_preapprovals
Revises: 0010_memory_research
Create Date: 2026-11-20
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0011_member_preapprovals"
down_revision: str | None = "0010_memory_research"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
CUR_USER = "nullif(current_setting('app.current_user_id', true), '')::uuid"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

FUNCTIONS = ("preapproval_find_user(text)", "preapproval_consume(uuid)")


def _app_role() -> str:
    value = (
        context.config.attributes.get("app_role")
        or context.get_x_argument(as_dictionary=True).get("app_role")
        or os.environ.get("POSTGRES_APP_USER")
        or "ukos_app"
    )
    if not ROLE_PATTERN.fullmatch(value):
        raise ValueError("invalid app_role name")
    return str(value)


SCHEMA_SQL = """
ALTER TABLE users ADD COLUMN email_verified boolean NOT NULL DEFAULT false;

CREATE TABLE org_member_preapprovals (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    email text NOT NULL,
    role text NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    consumed_at timestamptz,
    CONSTRAINT pk_org_member_preapprovals PRIMARY KEY (id),
    CONSTRAINT ck_org_member_preapprovals_email CHECK (email = lower(email) AND length(email) > 3),
    CONSTRAINT ck_org_member_preapprovals_role CHECK (role IN ('owner', 'admin', 'member')),
    CONSTRAINT fk_org_member_preapprovals_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_org_member_preapprovals_created_by FOREIGN KEY (created_by) REFERENCES users (id)
);
CREATE UNIQUE INDEX uq_org_member_preapprovals_pending
    ON org_member_preapprovals (org_id, lower(email)) WHERE consumed_at IS NULL;
CREATE INDEX ix_org_member_preapprovals_email_pending
    ON org_member_preapprovals (lower(email)) WHERE consumed_at IS NULL;
"""

FUNCTIONS_SQL = f"""
CREATE FUNCTION preapproval_find_user(p_email text) RETURNS uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    found uuid;
    n integer;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.organization_memberships m
        WHERE m.org_id = {CUR_ORG} AND m.user_id = {CUR_USER} AND m.role IN ('owner', 'admin')
    ) THEN
        RETURN NULL;
    END IF;
    SELECT count(*), min(u.id::text)::uuid INTO n, found
    FROM public.users u WHERE lower(u.email) = lower(p_email) AND u.email_verified;
    IF n = 1 THEN
        RETURN found;
    END IF;
    RETURN NULL;
END $$;

CREATE FUNCTION preapproval_consume(p_user_id uuid) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    u_email text;
    r record;
    consumed integer := 0;
BEGIN
    IF p_user_id IS NULL OR p_user_id IS DISTINCT FROM {CUR_USER} THEN
        RETURN 0;
    END IF;
    SELECT lower(u.email) INTO u_email
    FROM public.users u WHERE u.id = p_user_id AND u.email_verified AND u.email IS NOT NULL;
    IF u_email IS NULL THEN
        RETURN 0;
    END IF;
    FOR r IN
        WITH upd AS (
            UPDATE public.org_member_preapprovals p SET consumed_at = now()
            FROM public.organizations o
            WHERE p.consumed_at IS NULL AND lower(p.email) = u_email
              AND o.id = p.org_id AND o.deleted_at IS NULL
            RETURNING p.org_id, p.role
        ) SELECT upd.org_id, upd.role FROM upd
    LOOP
        INSERT INTO public.organization_memberships (org_id, user_id, role)
        VALUES (r.org_id, p_user_id, r.role) ON CONFLICT (org_id, user_id) DO NOTHING;
        IF FOUND THEN
            INSERT INTO public.audit_events (id, org_id, actor_user_id, event_type, target_type,
                                             target_id, outcome, metadata)
            VALUES (gen_random_uuid(), r.org_id, p_user_id, 'org.member.added', 'user',
                    p_user_id::text, 'success',
                    jsonb_build_object('email', u_email, 'from_role', NULL, 'to_role', r.role,
                                       'via', 'preapproval'));
        END IF;
        consumed := consumed + 1;
    END LOOP;
    RETURN consumed;
END $$;
"""

RLS_SQL = f"""
CREATE POLICY org_member_preapprovals_select ON org_member_preapprovals FOR SELECT
    USING (org_id = {CUR_ORG});
CREATE POLICY org_member_preapprovals_insert ON org_member_preapprovals FOR INSERT
    WITH CHECK (org_id = {CUR_ORG} AND created_by = {CUR_USER} AND consumed_at IS NULL);
CREATE POLICY org_member_preapprovals_delete ON org_member_preapprovals FOR DELETE
    USING (org_id = {CUR_ORG} AND consumed_at IS NULL);
"""


def upgrade() -> None:
    app = _app_role()
    op.execute(SCHEMA_SQL)
    op.execute("ALTER TABLE org_member_preapprovals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE org_member_preapprovals FORCE ROW LEVEL SECURITY")
    op.execute(FUNCTIONS_SQL)
    for signature in FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f'GRANT EXECUTE ON FUNCTION {signature} TO "{app}"')
    op.execute(RLS_SQL)
    op.execute(f'GRANT SELECT, INSERT, DELETE ON org_member_preapprovals TO "{app}"')


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS org_member_preapprovals")
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS email_verified")
