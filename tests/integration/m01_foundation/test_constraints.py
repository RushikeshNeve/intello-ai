"""M01.02.01 acceptance: unique organizations and owner-membership constraints pass.

Also covers workspace-membership integrity and the append-only audit log.
"""

from __future__ import annotations

import uuid

import pytest
from m01_seed import (
    add_member,
    constraint_name,
    insert_org,
    insert_user,
    insert_workspace,
    new_slug,
    seed_tenant,
    sqlstate,
)
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = pytest.mark.usefixtures("migrated")


def _owner_violation(exc: pytest.ExceptionInfo[DBAPIError]) -> None:
    assert sqlstate(exc.value) == "23514"
    assert constraint_name(exc.value) == "organization_has_owner"
    assert "last_owner" in str(exc.value.orig)


# --- unique organizations ---------------------------------------------------------------------


def test_duplicate_org_slug_rejected(admin_engine: Engine) -> None:
    slug = new_slug()
    with admin_engine.begin() as conn:
        user = insert_user(conn)
        org = insert_org(conn, created_by=user, slug=slug)
        add_member(conn, org, user, "owner")
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        org2 = insert_org(conn, created_by=user, slug=slug)
        add_member(conn, org2, user, "owner")
    assert constraint_name(exc.value) == "uq_organizations_slug"


def test_org_slug_differing_only_in_case_rejected(admin_engine: Engine) -> None:
    slug = new_slug()
    with admin_engine.begin() as conn:
        user = insert_user(conn)
        org = insert_org(conn, created_by=user, slug=slug)
        add_member(conn, org, user, "owner")
    # Stored slugs are lowercase-only, so a case variant can never coexist with the original.
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        insert_org(conn, created_by=user, slug=slug.upper())
    assert constraint_name(exc.value) == "ck_organizations_slug_format"


@pytest.mark.parametrize("slug", ["a", "-abc", "has space", "under_score", "x" * 64, "ÄBC"])
def test_invalid_org_slug_rejected(admin_engine: Engine, slug: str) -> None:
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        user = insert_user(conn)
        insert_org(conn, created_by=user, slug=slug)
    assert constraint_name(exc.value) == "ck_organizations_slug_format"


def test_duplicate_identity_rejected(admin_engine: Engine) -> None:
    subject = f"dup-{uuid.uuid4()}"
    with admin_engine.begin() as conn:
        insert_user(conn, subject=subject)
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        insert_user(conn, subject=subject)
    assert constraint_name(exc.value) == "uq_users_issuer_subject"


def test_duplicate_workspace_slug_in_org_rejected_but_allowed_across_orgs(
    admin_engine: Engine,
) -> None:
    a, b = seed_tenant(admin_engine), seed_tenant(admin_engine)
    with admin_engine.connect() as conn:
        slug = conn.execute(
            text("SELECT slug FROM workspaces WHERE id = :w"), {"w": a.workspace_id}
        ).scalar_one()
    with admin_engine.begin() as conn:  # same slug, other org: fine
        conn.execute(
            text(
                "INSERT INTO workspaces (id, org_id, slug, name, created_by)"
                " VALUES (:id, :o, :s, 'W', :u)"
            ),
            {"id": uuid.uuid4(), "o": b.org_id, "s": slug, "u": b.owner_id},
        )
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspaces (id, org_id, slug, name, created_by)"
                " VALUES (:id, :o, :s, 'W', :u)"
            ),
            {"id": uuid.uuid4(), "o": a.org_id, "s": slug, "u": a.owner_id},
        )
    assert constraint_name(exc.value) == "uq_workspaces_org_id_slug"


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("organization_memberships", "role", "superuser"),
        ("workspace_memberships", "role", "owner"),
        ("resource_grants", "permission", "delete"),
        ("audit_events", "outcome", "maybe"),
    ],
)
def test_enum_checks(admin_engine: Engine, table: str, column: str, value: str) -> None:
    t = seed_tenant(admin_engine)
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        if table == "audit_events":
            conn.execute(
                text(
                    "INSERT INTO audit_events (id, event_type, target_type, target_id, outcome)"
                    " VALUES (:id, 'x', 'y', 'z', :v)"
                ),
                {"id": uuid.uuid4(), "v": value},
            )
        else:
            conn.execute(
                text(f"UPDATE {table} SET {column} = :v WHERE org_id = :o"),  # noqa: S608
                {"v": value, "o": t.org_id},
            )
    assert sqlstate(exc.value) == "23514"
    assert (constraint_name(exc.value) or "").startswith(f"ck_{table}_")


# --- owner membership -------------------------------------------------------------------------


def test_org_without_owner_rejected_at_commit(admin_engine: Engine) -> None:
    with admin_engine.begin() as conn:
        user = insert_user(conn)
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        org = insert_org(conn, created_by=user)
        add_member(conn, org, user, "admin")  # a member, but not an owner
        # Deferred: nothing fails until COMMIT.
        count = conn.execute(
            text("SELECT count(*) FROM organizations WHERE id = :o"), {"o": org}
        ).scalar_one()
        assert count == 1
    _owner_violation(exc)


def test_org_created_with_owner_in_same_transaction_commits(admin_engine: Engine) -> None:
    with admin_engine.begin() as conn:
        user = insert_user(conn)
        org = insert_org(conn, created_by=user)  # owner added later in the same transaction
        add_member(conn, org, user, "owner")
    with admin_engine.connect() as conn:
        role = conn.execute(
            text("SELECT role FROM organization_memberships WHERE org_id = :o"), {"o": org}
        ).scalar_one()
    assert role == "owner"


def test_demoting_last_owner_rejected(admin_engine: Engine) -> None:
    t = seed_tenant(admin_engine)
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE organization_memberships SET role = 'admin' WHERE org_id = :o"),
            {"o": t.org_id},
        )
    _owner_violation(exc)


def test_removing_last_owner_rejected(admin_engine: Engine) -> None:
    t = seed_tenant(admin_engine)
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM organization_memberships WHERE org_id = :o"), {"o": t.org_id}
        )
    _owner_violation(exc)


def test_with_two_owners_removing_or_demoting_one_succeeds(admin_engine: Engine) -> None:
    t = seed_tenant(admin_engine)
    with admin_engine.begin() as conn:
        second = insert_user(conn)
        third = insert_user(conn)
        add_member(conn, t.org_id, second, "owner")
        add_member(conn, t.org_id, third, "owner")
    with admin_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM organization_memberships WHERE org_id = :o AND user_id = :u"),
            {"o": t.org_id, "u": t.owner_id},
        )
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE organization_memberships SET role = 'member'"
                " WHERE org_id = :o AND user_id = :u"
            ),
            {"o": t.org_id, "u": second},
        )
    with admin_engine.connect() as conn:
        owners = conn.execute(
            text(
                "SELECT user_id FROM organization_memberships WHERE org_id = :o AND role = 'owner'"
            ),
            {"o": t.org_id},
        ).scalars()
        assert list(owners) == [third]


def test_ownership_transfer_in_one_transaction_succeeds(admin_engine: Engine) -> None:
    t = seed_tenant(admin_engine)
    with admin_engine.begin() as conn:
        heir = insert_user(conn)
        conn.execute(
            text("DELETE FROM organization_memberships WHERE org_id = :o"), {"o": t.org_id}
        )
        add_member(conn, t.org_id, heir, "owner")


def test_soft_deleted_org_may_lose_its_owner(admin_engine: Engine) -> None:
    t = seed_tenant(admin_engine)
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE organizations SET deleted_at = now() WHERE id = :o"), {"o": t.org_id}
        )
        conn.execute(
            text("DELETE FROM organization_memberships WHERE org_id = :o"), {"o": t.org_id}
        )
    # ...but cannot be restored without one.
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE organizations SET deleted_at = NULL WHERE id = :o"), {"o": t.org_id}
        )
    _owner_violation(exc)


# --- workspace membership integrity -----------------------------------------------------------


def test_workspace_membership_for_non_org_member_rejected(admin_engine: Engine) -> None:
    t = seed_tenant(admin_engine)
    with admin_engine.begin() as conn:
        outsider = insert_user(conn)
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspace_memberships (workspace_id, org_id, user_id, role)"
                " VALUES (:w, :o, :u, 'viewer')"
            ),
            {"w": t.workspace_id, "o": t.org_id, "u": outsider},
        )
    assert constraint_name(exc.value) == "fk_workspace_memberships_org_member"


def test_workspace_membership_org_mismatch_rejected(admin_engine: Engine) -> None:
    a, b = seed_tenant(admin_engine), seed_tenant(admin_engine)
    # b's owner is a member of org b; claim org b on a workspace of org a.
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspace_memberships (workspace_id, org_id, user_id, role)"
                " VALUES (:w, :o, :u, 'viewer')"
            ),
            {"w": a.workspace_id, "o": b.org_id, "u": b.owner_id},
        )
    assert constraint_name(exc.value) == "fk_workspace_memberships_workspace"


def test_removing_org_member_removes_workspace_memberships(admin_engine: Engine) -> None:
    t = seed_tenant(admin_engine)
    with admin_engine.begin() as conn:
        member = insert_user(conn)
        add_member(conn, t.org_id, member, "member")
        conn.execute(
            text(
                "INSERT INTO workspace_memberships (workspace_id, org_id, user_id, role)"
                " VALUES (:w, :o, :u, 'editor')"
            ),
            {"w": t.workspace_id, "o": t.org_id, "u": member},
        )
    with admin_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM organization_memberships WHERE org_id = :o AND user_id = :u"),
            {"o": t.org_id, "u": member},
        )
        left = conn.execute(
            text("SELECT count(*) FROM workspace_memberships WHERE user_id = :u"), {"u": member}
        ).scalar_one()
    assert left == 0


def test_grant_workspace_org_mismatch_rejected(admin_engine: Engine) -> None:
    a, b = seed_tenant(admin_engine), seed_tenant(admin_engine)
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO resource_grants (id, org_id, workspace_id, resource_type, resource_id,"
                " grantee_type, grantee_id, permission, granted_by)"
                " VALUES (:id, :o, :w, 'document', :r, 'user', :g, 'read', :u)"
            ),
            {
                "id": uuid.uuid4(),
                "o": b.org_id,
                "w": a.workspace_id,
                "r": uuid.uuid4(),
                "g": str(b.owner_id),
                "u": b.owner_id,
            },
        )
    assert constraint_name(exc.value) == "fk_resource_grants_workspace"


def test_workspace_requires_existing_org(admin_engine: Engine) -> None:
    with admin_engine.begin() as conn:
        user = insert_user(conn)
    with pytest.raises(IntegrityError), admin_engine.begin() as conn:
        insert_workspace(conn, uuid.uuid4(), user)


# --- audit_events append-only -----------------------------------------------------------------


@pytest.mark.parametrize("statement", ["UPDATE", "DELETE"])
@pytest.mark.parametrize("as_app", [False, True], ids=["admin", "app_role"])
def test_audit_events_are_append_only(
    admin_engine: Engine, app_engine: Engine, statement: str, as_app: bool
) -> None:
    t = seed_tenant(admin_engine)
    sql = (
        "UPDATE audit_events SET outcome = 'error' WHERE org_id = :o"
        if statement == "UPDATE"
        else "DELETE FROM audit_events WHERE org_id = :o"
    )
    engine = app_engine if as_app else admin_engine
    with pytest.raises(DBAPIError) as exc, engine.begin() as conn:
        # The app role gets the tenant context, so RLS is not what stops it.
        conn.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(t.org_id)}
        )
        conn.execute(text(sql), {"o": t.org_id})
    assert sqlstate(exc.value) == "42501"
    if not as_app:
        assert "append-only" in str(exc.value.orig)
    with admin_engine.connect() as conn:
        outcome = conn.execute(
            text("SELECT outcome FROM audit_events WHERE org_id = :o"), {"o": t.org_id}
        ).scalar_one()
    assert outcome == "success"


def test_audit_events_truncate_rejected_for_admin(admin_engine: Engine) -> None:
    seed_tenant(admin_engine)
    with pytest.raises(DBAPIError) as exc, admin_engine.begin() as conn:
        conn.execute(text("TRUNCATE audit_events"))
    assert sqlstate(exc.value) == "42501"
