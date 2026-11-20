"""``users``: visible = self + co-members of the current org; writes only to self (DC-014)."""

from __future__ import annotations

import uuid

from rls_world import ISSUER, World, admin_count, app_tx, expect_denied, scalar
from sqlalchemy import Engine, text

from app.core.db import set_identity_context, tenant_session


def _visible(conn: object) -> set[uuid.UUID]:
    return set(conn.execute(text("SELECT id FROM users")).scalars())  # type: ignore[attr-defined]


def test_org_context_sees_self_and_co_members_only(app_engine: Engine, world: World) -> None:
    a = world.a
    with app_tx(app_engine, user=a.owner_id, org=a.org_id) as conn:
        assert _visible(conn) == {a.owner_id, a.member_id, world.shared_id}


def test_shared_member_sees_each_org_separately(app_engine: Engine, world: World) -> None:
    with app_tx(app_engine, user=world.shared_id, org=world.a.org_id) as conn:
        assert _visible(conn) == {world.a.owner_id, world.a.member_id, world.shared_id}
    with app_tx(app_engine, user=world.shared_id, org=world.b.org_id) as conn:
        assert _visible(conn) == {world.b.owner_id, world.b.member_id, world.shared_id}


def test_user_context_without_org_sees_self_only(app_engine: Engine, world: World) -> None:
    with app_tx(app_engine, user=world.shared_id) as conn:
        assert _visible(conn) == {world.shared_id}


def test_outsider_sees_self_only(app_engine: Engine, world: World) -> None:
    with app_tx(app_engine, user=world.outsider_id, org=world.a.org_id) as conn:
        assert _visible(conn) == {world.outsider_id}


def test_b_users_by_id_subject_or_email_invisible(app_engine: Engine, world: World) -> None:
    b = world.b
    with app_tx(app_engine, user=world.a.owner_id, org=world.a.org_id) as conn:
        for sql, v in (
            ("SELECT count(*) FROM users WHERE id = :v", b.owner_id),
            ("SELECT count(*) FROM users WHERE subject = :v", world.subject(b.owner_id)),
            ("SELECT count(*) FROM users WHERE email = :v", "b-owner@example.test"),
            ("SELECT count(*) FROM users WHERE display_name LIKE :v", "b-%"),
        ):
            assert scalar(conn, sql, {"v": v}) == 0, sql


def test_identity_context_sees_only_that_identity(app_engine: Engine, world: World) -> None:
    b_sub = world.subject(world.b.owner_id)
    with app_tx(app_engine, issuer=ISSUER, subject=b_sub) as conn:
        assert _visible(conn) == {world.b.owner_id}
    with app_tx(app_engine, issuer="http://other-issuer.test", subject=b_sub) as conn:
        assert _visible(conn) == set()  # issuer is part of the identity


def test_identity_upsert_cannot_touch_other_identity(
    app_engine: Engine, admin_engine: Engine, world: World
) -> None:
    """Provisioning as identity X, an upsert carrying B.owner's (issuer, subject) is rejected
    (proposed row fails WITH CHECK; the conflicting row fails the UPDATE USING clause)."""
    me = f"me-{uuid.uuid4()}"
    b_sub = world.subject(world.b.owner_id)
    for stmt in (
        "INSERT INTO users (id, issuer, subject, email) VALUES (:id, :iss, :sub, 'pwn@x')"
        " ON CONFLICT (issuer, subject) DO UPDATE SET email = excluded.email",
        "INSERT INTO users (id, issuer, subject) VALUES (:id, :iss, :sub)",
    ):
        expect_denied(
            app_engine,
            stmt,
            {"id": uuid.uuid4(), "iss": ISSUER, "sub": b_sub},
            states=("42501",),
            match="row-level security",
            issuer=ISSUER,
            subject=me,
        )
    with app_tx(app_engine, issuer=ISSUER, subject=me) as conn:
        r = conn.execute(text("UPDATE users SET email = 'pwn@x' WHERE subject = :s"), {"s": b_sub})
        assert r.rowcount == 0
    assert (
        admin_count(
            admin_engine,
            "SELECT count(*) FROM users WHERE id = :u AND email = 'b-owner@example.test'",
            {"u": world.b.owner_id},
        )
        == 1
    )


def test_own_identity_upsert_cannot_be_renamed_to_another(app_engine: Engine, world: World) -> None:
    """Identity-only context: the self row cannot be re-pointed at someone else's subject."""
    me = f"me-{uuid.uuid4()}"
    with tenant_session(user_id=None, org_id=None, engine=app_engine) as s:
        set_identity_context(s, issuer=ISSUER, subject=me)
        s.execute(
            text("INSERT INTO users (id, issuer, subject) VALUES (:id, :iss, :sub)"),
            {"id": uuid.uuid4(), "iss": ISSUER, "sub": me},
        )
    # Denied with 42501 either by RLS WITH CHECK or, since migration 0002, earlier by the
    # users_identity_immutable BEFORE UPDATE trigger (DC-016). Both must remain denials.
    err = expect_denied(
        app_engine,
        "UPDATE users SET subject = :new WHERE subject = :me",
        {"new": f"unclaimed-{uuid.uuid4()}", "me": me},
        issuer=ISSUER,
        subject=me,
    )
    message = str(err.orig)
    assert "row-level security" in message or "immutable" in message, message


def test_insert_for_other_user_id_rejected(app_engine: Engine, world: World) -> None:
    """With user context A, inserting a row that is neither A's id nor A's identity fails."""
    expect_denied(
        app_engine,
        "INSERT INTO users (id, issuer, subject) VALUES (:id, :iss, :sub)",
        {"id": uuid.uuid4(), "iss": ISSUER, "sub": f"other-{uuid.uuid4()}"},
        match="row-level security",
        user=world.a.owner_id,
        org=world.a.org_id,
    )


def test_update_co_member_rejected(app_engine: Engine, admin_engine: Engine, world: World) -> None:
    """Co-members are readable, never writable."""
    a = world.a
    with app_tx(app_engine, user=a.owner_id, org=a.org_id) as conn:
        r = conn.execute(
            text("UPDATE users SET display_name = 'pwned' WHERE id IN (:m, :s)"),
            {"m": a.member_id, "s": world.shared_id},
        )
        assert r.rowcount == 0
        r = conn.execute(text("UPDATE users SET last_seen_at = now()"))
        assert r.rowcount == 1  # self only


def test_delete_users_never_allowed(app_engine: Engine, world: World) -> None:
    expect_denied(
        app_engine,
        "DELETE FROM users WHERE id = :u",
        {"u": world.a.owner_id},
        match="permission denied",
        user=world.a.owner_id,
        org=world.a.org_id,
    )
