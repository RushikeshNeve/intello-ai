"""RLS hardening from the M01.03.02 adversarial suite (DC-016).

1. audit_events_insert: an org-scoped audit row may only name the CURRENT user (or no user) as
   actor, so a tenant cannot forge events attributed to someone else.
2. ukos_assert_org_has_owner (SECURITY DEFINER): search_path pins pg_temp LAST, so temporary
   objects can never shadow catalog/public names inside the definer function.
3. users.issuer / users.subject are immutable after insert, so a user cannot re-point their own
   row to another (unclaimed) external identity.

Revision ID: 0002_rls_hardening
Revises: 0001_identity_tenancy
"""

from __future__ import annotations

from alembic import op

revision: str = "0002_rls_hardening"
down_revision: str | None = "0001_identity_tenancy"
branch_labels = None
depends_on = None

CUR_USER = "nullif(current_setting('app.current_user_id', true), '')::uuid"
CUR_ORG = "nullif(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("DROP POLICY audit_events_insert ON audit_events")
    op.execute(
        f"""
CREATE POLICY audit_events_insert ON audit_events FOR INSERT WITH CHECK (
    (org_id = {CUR_ORG} AND (actor_user_id IS NULL OR actor_user_id = {CUR_USER}))
    OR (org_id IS NULL AND (actor_user_id IS NULL OR actor_user_id = {CUR_USER}))
)"""
    )
    op.execute(
        "ALTER FUNCTION ukos_assert_org_has_owner() SET search_path = pg_catalog, public, pg_temp"
    )
    op.execute(
        """
CREATE FUNCTION ukos_users_identity_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NEW.issuer IS DISTINCT FROM OLD.issuer OR NEW.subject IS DISTINCT FROM OLD.subject THEN
        RAISE EXCEPTION 'users.issuer/subject are immutable'
            USING ERRCODE = '42501', CONSTRAINT = 'users_identity_immutable';
    END IF;
    RETURN NEW;
END;
$$"""
    )
    op.execute("REVOKE ALL ON FUNCTION ukos_users_identity_immutable() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER users_identity_immutable BEFORE UPDATE OF issuer, subject ON users "
        "FOR EACH ROW EXECUTE FUNCTION ukos_users_identity_immutable()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS users_identity_immutable ON users")
    op.execute("DROP FUNCTION IF EXISTS ukos_users_identity_immutable()")
    op.execute("ALTER FUNCTION ukos_assert_org_has_owner() SET search_path = pg_catalog, public")
    op.execute("DROP POLICY audit_events_insert ON audit_events")
    op.execute(
        f"""
CREATE POLICY audit_events_insert ON audit_events FOR INSERT WITH CHECK (
    org_id = {CUR_ORG}
    OR (org_id IS NULL AND (actor_user_id IS NULL OR actor_user_id = {CUR_USER}))
)"""
    )
