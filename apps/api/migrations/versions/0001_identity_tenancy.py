"""Identity and tenancy schema: users, orgs, workspaces, memberships, grants, audit (M01.02.01).

Contract: docs/architecture/m01-contracts.md §2 (ADR-013, ADR-028).

* Constraints: unique (issuer, subject); globally unique lowercase org slug (the format check
  only admits ``[a-z0-9-]``, so uniqueness is case-insensitive); unique (org_id, slug) for
  workspaces; role / permission / outcome enums as CHECKs; composite FKs pin
  ``workspace_memberships.org_id`` and ``resource_grants.org_id`` to the workspace's org, and a
  workspace member must be an org member.
* "Every live org has >= 1 owner": DEFERRABLE INITIALLY DEFERRED constraint triggers, checked at
  commit. Violation -> SQLSTATE 23514, constraint ``organization_has_owner``, message
  ``last_owner: ...``.
* ``audit_events`` is append-only: the app role gets INSERT and SELECT only, and a trigger rejects
  UPDATE / DELETE / TRUNCATE for every role (SQLSTATE 42501).
* RLS ENABLE + FORCE on every table, fail-closed: context comes from transaction-local
  ``app.current_user_id`` / ``app.current_org_id`` (plus ``app.current_issuer`` /
  ``app.current_subject`` so the provisioner can upsert its own ``users`` row by external
  identity before the internal id is known).
* Grants: minimum DML to the app role (``-x app_role=`` / env POSTGRES_APP_USER, default
  ``ukos_app``).

Revision ID: 0001_identity_tenancy
Revises:
Create Date: 2026-11-16
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import context, op

revision: str = "0001_identity_tenancy"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SLUG = "^[a-z0-9][a-z0-9-]{1,62}$"
UUID_TEXT = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

CUR_USER = "nullif(current_setting('app.current_user_id', true), '')::uuid"
CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"
CUR_ISSUER = "nullif(current_setting('app.current_issuer', true), '')"
CUR_SUBJECT = "nullif(current_setting('app.current_subject', true), '')"

TABLES = (
    "users",
    "organizations",
    "organization_memberships",
    "workspaces",
    "workspace_memberships",
    "resource_grants",
    "audit_events",
)


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
CREATE TABLE users (
    id uuid NOT NULL,
    issuer text NOT NULL,
    subject text NOT NULL,
    email text,
    display_name text,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_users PRIMARY KEY (id),
    CONSTRAINT uq_users_issuer_subject UNIQUE (issuer, subject),
    CONSTRAINT ck_users_issuer_not_empty CHECK (length(issuer) > 0),
    CONSTRAINT ck_users_subject_not_empty CHECK (length(subject) > 0)
);

CREATE TABLE organizations (
    id uuid NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    CONSTRAINT pk_organizations PRIMARY KEY (id),
    CONSTRAINT uq_organizations_slug UNIQUE (slug),
    CONSTRAINT ck_organizations_slug_format CHECK (slug ~ '{SLUG}'),
    CONSTRAINT ck_organizations_name_not_empty CHECK (length(btrim(name)) > 0),
    CONSTRAINT fk_organizations_created_by FOREIGN KEY (created_by) REFERENCES users (id)
);

CREATE TABLE organization_memberships (
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_organization_memberships PRIMARY KEY (org_id, user_id),
    CONSTRAINT ck_organization_memberships_role CHECK (role IN ('owner', 'admin', 'member')),
    CONSTRAINT fk_organization_memberships_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_organization_memberships_user_id FOREIGN KEY (user_id) REFERENCES users (id)
);
CREATE INDEX ix_organization_memberships_user_id ON organization_memberships (user_id);

CREATE TABLE workspaces (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    slug text NOT NULL,
    name text NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    CONSTRAINT pk_workspaces PRIMARY KEY (id),
    CONSTRAINT uq_workspaces_org_id_slug UNIQUE (org_id, slug),
    CONSTRAINT uq_workspaces_id_org_id UNIQUE (id, org_id),
    CONSTRAINT ck_workspaces_slug_format CHECK (slug ~ '{SLUG}'),
    CONSTRAINT ck_workspaces_name_not_empty CHECK (length(btrim(name)) > 0),
    CONSTRAINT fk_workspaces_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_workspaces_created_by FOREIGN KEY (created_by) REFERENCES users (id)
);

CREATE TABLE workspace_memberships (
    workspace_id uuid NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_workspace_memberships PRIMARY KEY (workspace_id, user_id),
    CONSTRAINT ck_workspace_memberships_role CHECK (role IN ('admin', 'editor', 'viewer')),
    CONSTRAINT fk_workspace_memberships_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_workspace_memberships_org_member FOREIGN KEY (org_id, user_id)
        REFERENCES organization_memberships (org_id, user_id) ON DELETE CASCADE,
    CONSTRAINT fk_workspace_memberships_user_id FOREIGN KEY (user_id) REFERENCES users (id)
);
CREATE INDEX ix_workspace_memberships_org_id_user_id
    ON workspace_memberships (org_id, user_id);

CREATE TABLE resource_grants (
    id uuid NOT NULL,
    org_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    resource_type text NOT NULL,
    resource_id uuid NOT NULL,
    grantee_type text NOT NULL,
    grantee_id text NOT NULL,
    permission text NOT NULL,
    granted_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    revoked_at timestamptz,
    revoked_by uuid,
    CONSTRAINT pk_resource_grants PRIMARY KEY (id),
    CONSTRAINT ck_resource_grants_resource_type CHECK (resource_type IN ('document')),
    CONSTRAINT ck_resource_grants_grantee_type CHECK (grantee_type IN ('user', 'workspace_role')),
    CONSTRAINT ck_resource_grants_grantee_id CHECK (
        (grantee_type = 'user' AND grantee_id ~ '{UUID_TEXT}')
        OR (grantee_type = 'workspace_role' AND grantee_id IN ('admin', 'editor', 'viewer'))
    ),
    CONSTRAINT ck_resource_grants_permission CHECK (permission IN ('read', 'write', 'manage')),
    CONSTRAINT ck_resource_grants_revoked CHECK (revoked_at IS NOT NULL OR revoked_by IS NULL),
    CONSTRAINT fk_resource_grants_org_id FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_resource_grants_workspace FOREIGN KEY (workspace_id, org_id)
        REFERENCES workspaces (id, org_id) ON DELETE CASCADE,
    CONSTRAINT fk_resource_grants_granted_by FOREIGN KEY (granted_by) REFERENCES users (id),
    CONSTRAINT fk_resource_grants_revoked_by FOREIGN KEY (revoked_by) REFERENCES users (id)
);
CREATE INDEX ix_resource_grants_resource ON resource_grants (org_id, resource_type, resource_id);

CREATE TABLE audit_events (
    id uuid NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    org_id uuid,
    actor_user_id uuid,
    event_type text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    outcome text NOT NULL,
    request_id text,
    metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    CONSTRAINT pk_audit_events PRIMARY KEY (id),
    CONSTRAINT ck_audit_events_outcome CHECK (outcome IN ('success', 'denied', 'error')),
    CONSTRAINT ck_audit_events_event_type_not_empty CHECK (length(event_type) > 0)
);
CREATE INDEX ix_audit_events_org_id_occurred_at ON audit_events (org_id, occurred_at);
"""

INTEGRITY_SQL = """
-- Every live (not soft-deleted) organization keeps at least one owner. SECURITY DEFINER so the
-- check sees every membership row regardless of the caller's RLS context.
CREATE FUNCTION ukos_assert_org_has_owner() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    target uuid;
BEGIN
    IF TG_TABLE_NAME = 'organizations' THEN
        target := NEW.id;
    ELSE
        target := OLD.org_id;
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.organizations o WHERE o.id = target AND o.deleted_at IS NULL
    ) AND NOT EXISTS (
        SELECT 1 FROM public.organization_memberships m
        WHERE m.org_id = target AND m.role = 'owner'
    ) THEN
        RAISE EXCEPTION 'last_owner: organization % must have at least one owner', target
            USING ERRCODE = '23514', CONSTRAINT = 'organization_has_owner';
    END IF;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION ukos_assert_org_has_owner() FROM PUBLIC;

CREATE CONSTRAINT TRIGGER organizations_require_owner
    AFTER INSERT OR UPDATE OF deleted_at ON organizations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ukos_assert_org_has_owner();

CREATE CONSTRAINT TRIGGER organization_memberships_require_owner
    AFTER UPDATE OR DELETE ON organization_memberships
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ukos_assert_org_has_owner();

-- audit_events is append-only for every role, including the owner/admin.
CREATE FUNCTION ukos_audit_events_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (% rejected)', TG_OP
        USING ERRCODE = '42501';
END;
$$;
REVOKE ALL ON FUNCTION ukos_audit_events_append_only() FROM PUBLIC;

CREATE TRIGGER audit_events_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION ukos_audit_events_append_only();

CREATE TRIGGER audit_events_no_truncate
    BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION ukos_audit_events_append_only();
"""

_SELF_USER = f"(id = {CUR_USER} OR (issuer = {CUR_ISSUER} AND subject = {CUR_SUBJECT}))"

RLS_SQL = f"""
-- users: self (by internal id or verified external identity), or co-members of current org.
CREATE POLICY users_select ON users FOR SELECT USING (
    {_SELF_USER}
    OR (
        EXISTS (SELECT 1 FROM organization_memberships m
                WHERE m.org_id = {CUR_ORG} AND m.user_id = users.id)
        AND EXISTS (SELECT 1 FROM organization_memberships me
                    WHERE me.org_id = {CUR_ORG} AND me.user_id = {CUR_USER})
    )
);
CREATE POLICY users_insert ON users FOR INSERT WITH CHECK {_SELF_USER};
CREATE POLICY users_update ON users FOR UPDATE USING {_SELF_USER} WITH CHECK {_SELF_USER};

-- organizations: the current org, or any org the current user belongs to.
CREATE POLICY organizations_select ON organizations FOR SELECT USING (
    id = {CUR_ORG}
    OR EXISTS (SELECT 1 FROM organization_memberships m
               WHERE m.org_id = organizations.id AND m.user_id = {CUR_USER})
);
CREATE POLICY organizations_insert ON organizations FOR INSERT
    WITH CHECK (created_by = {CUR_USER});
CREATE POLICY organizations_update ON organizations FOR UPDATE
    USING (id = {CUR_ORG}) WITH CHECK (id = {CUR_ORG});

-- organization_memberships: rows of the current org, or the current user's own rows.
CREATE POLICY organization_memberships_select ON organization_memberships FOR SELECT
    USING (org_id = {CUR_ORG} OR user_id = {CUR_USER});
CREATE POLICY organization_memberships_insert ON organization_memberships FOR INSERT
    WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY organization_memberships_update ON organization_memberships FOR UPDATE
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY organization_memberships_delete ON organization_memberships FOR DELETE
    USING (org_id = {CUR_ORG});

CREATE POLICY workspaces_org ON workspaces FOR ALL
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY workspace_memberships_org ON workspace_memberships FOR ALL
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});
CREATE POLICY resource_grants_org ON resource_grants FOR ALL
    USING (org_id = {CUR_ORG}) WITH CHECK (org_id = {CUR_ORG});

-- audit_events: read only the current org's events. Inserts must target the current org, or be
-- org-less events (e.g. user.provisioned) by the current user or by no user (system).
CREATE POLICY audit_events_select ON audit_events FOR SELECT USING (org_id = {CUR_ORG});
CREATE POLICY audit_events_insert ON audit_events FOR INSERT WITH CHECK (
    org_id = {CUR_ORG}
    OR (org_id IS NULL AND (actor_user_id IS NULL OR actor_user_id = {CUR_USER}))
);
"""


def _grants_sql(role: str) -> str:
    return f"""
GRANT SELECT, INSERT, UPDATE ON users, organizations, workspaces, resource_grants TO "{role}";
GRANT SELECT, INSERT, UPDATE, DELETE ON organization_memberships, workspace_memberships
    TO "{role}";
GRANT SELECT, INSERT ON audit_events TO "{role}";
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)
    op.execute(INTEGRITY_SQL)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(RLS_SQL)
    op.execute(_grants_sql(_app_role()))


def downgrade() -> None:
    # Policies that reference organization_memberships from other tables go first; dropping a
    # table then drops its own policies, triggers, indexes and grants.
    op.execute("DROP POLICY IF EXISTS users_select ON users")
    op.execute("DROP POLICY IF EXISTS organizations_select ON organizations")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP FUNCTION IF EXISTS ukos_audit_events_append_only()")
    op.execute("DROP FUNCTION IF EXISTS ukos_assert_org_has_owner()")
