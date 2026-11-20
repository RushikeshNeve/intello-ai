"""Context spoofing and context lifetime.

RLS trusts ``app.current_*``: whoever can run SQL as ``ukos_app`` can name any org. The
isolation guarantee therefore rests on (1) the API being the only setter (it derives the org from
the authenticated principal and memberships, ADR-027) and (2) the value never outliving its
transaction. These tests document (1) and prove (2).
"""

from __future__ import annotations

import uuid

import pytest
from rls_world import World, app_tx, expect_denied, scalar, set_ctx
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

from app.core.db import set_identity_context, tenant_session

# -- spoofing (documented behaviour) ----------------------------------------------------------


def test_spoofed_org_context_is_trusted_documented(app_engine: Engine, world: World) -> None:
    """A.owner is NOT a member of B, yet with app.current_org_id = B the org-keyed tables show
    B's rows. This is the designed trust boundary (DB-level RLS keys on the session variable;
    membership is enforced by the API before it sets the variable). ``users`` is the exception:
    its co-member branch also requires the current user to be a member of the current org."""
    a, b = world.a, world.b
    with app_tx(app_engine, user=a.owner_id, org=b.org_id) as conn:
        assert set(conn.execute(text("SELECT org_id FROM workspaces")).scalars()) == {b.org_id}
        assert set(conn.execute(text("SELECT id FROM organizations")).scalars()) == {
            a.org_id,  # via own membership
            b.org_id,  # via spoofed context
        }
        users = set(conn.execute(text("SELECT id FROM users")).scalars())
        assert users == {a.owner_id}  # self only: B's members stay hidden


def test_spoofed_user_context_cannot_read_users_of_other_org(
    app_engine: Engine, world: World
) -> None:
    """user = outsider (no membership), org = B: users shows only the outsider."""
    with app_tx(app_engine, user=world.outsider_id, org=world.b.org_id) as conn:
        users = set(conn.execute(text("SELECT id FROM users")).scalars())
    assert users == {world.outsider_id}


def test_non_uuid_context_fails_closed(app_engine: Engine, world: World) -> None:
    """Garbage in the context is a hard error (22P02), never a widened predicate."""
    for bad in ("' OR true --", "*", "00000000-0000-0000-0000-00000000000g"):
        with app_engine.connect() as conn, conn.begin():
            conn.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": bad})
            with pytest.raises(Exception) as exc:
                conn.execute(text("SELECT count(*) FROM workspaces")).scalar()
            assert getattr(getattr(exc.value, "orig", None), "sqlstate", None) == "22P02"


def test_nil_uuid_context_sees_nothing(app_engine: Engine, world: World) -> None:
    nil = uuid.UUID(int=0)
    with app_tx(app_engine, user=nil, org=nil) as conn:
        for t in ("organizations", "workspaces", "users", "audit_events"):
            assert scalar(conn, f"SELECT count(*) FROM {t}") == 0, t


# -- transaction-local lifetime -----------------------------------------------------------------


def _ctx(conn: object) -> tuple[str, str, str, str]:
    row = conn.execute(  # type: ignore[attr-defined]
        text(
            "SELECT coalesce(current_setting('app.current_user_id', true), ''),"
            " coalesce(current_setting('app.current_org_id', true), ''),"
            " coalesce(current_setting('app.current_issuer', true), ''),"
            " coalesce(current_setting('app.current_subject', true), '')"
        )
    ).one()
    return tuple(row)  # type: ignore[return-value]


@pytest.mark.parametrize("end", ["commit", "rollback"])
def test_context_gone_after_transaction_end(app_engine: Engine, world: World, end: str) -> None:
    a = world.a
    with app_engine.connect() as conn:
        trans = conn.begin()
        set_ctx(conn, user=a.owner_id, org=a.org_id, issuer="http://issuer.test",
                subject=world.subject(a.owner_id))  # fmt: skip
        assert scalar(conn, "SELECT count(*) FROM workspaces") >= 1
        getattr(trans, end)()
        # Same physical connection, next transaction.
        with conn.begin():
            assert _ctx(conn) == ("", "", "", "")
            for t in ("organizations", "workspaces", "users", "organization_memberships"):
                assert scalar(conn, f"SELECT count(*) FROM {t}") == 0, t


def test_context_gone_after_tenant_session_error(app_engine: Engine, world: World) -> None:
    a = world.a

    class Boom(Exception):
        pass

    with (
        pytest.raises(Boom),
        tenant_session(user_id=a.owner_id, org_id=a.org_id, engine=app_engine) as session,
    ):
        set_identity_context(session, issuer="http://issuer.test", subject="x")
        raise Boom
    # The pool is small (2): check every pooled connection.
    conns = [app_engine.connect() for _ in range(2)]
    try:
        for conn in conns:
            assert _ctx(conn) == ("", "", "", "")
            conn.rollback()
    finally:
        for conn in conns:
            conn.close()


def test_savepoint_rollback_restores_outer_context(app_engine: Engine, world: World) -> None:
    a, b = world.a, world.b
    with app_tx(app_engine, user=a.owner_id, org=a.org_id) as conn:
        nested = conn.begin_nested()
        set_ctx(conn, user=b.owner_id, org=b.org_id)
        assert set(conn.execute(text("SELECT org_id FROM workspaces")).scalars()) == {b.org_id}
        nested.rollback()
        assert _ctx(conn)[:2] == (str(a.owner_id), str(a.org_id))
        assert set(conn.execute(text("SELECT org_id FROM workspaces")).scalars()) == {a.org_id}


def test_local_set_config_in_autocommit_lasts_one_statement(
    app_engine: Engine, world: World
) -> None:
    with app_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(world.a.org_id)}
        )
        assert _ctx(conn)[1] == ""
        assert scalar(conn, "SELECT count(*) FROM workspaces") == 0


# -- session-level set_config (noted) -----------------------------------------------------------


def _single_conn_engine(app_url: URL) -> Engine:
    return create_engine(app_url, pool_size=1, max_overflow=0)


def test_session_level_set_config_persists_on_pooled_connection_noted(
    app_url: URL, world: World
) -> None:
    """NOTED: any role may set its own custom GUCs at session level. ``set_config(.., false)`` /
    ``SET app.current_org_id`` survives COMMIT and the SQLAlchemy pool's reset-on-return
    (ROLLBACK, not DISCARD ALL), so it reaches the next checkout. app.core.db only ever uses
    ``is_local = true``; the API must keep it that way."""
    engine = _single_conn_engine(app_url)
    try:
        with engine.connect() as conn:
            conn.execute(
                text("SELECT set_config('app.current_org_id', :o, false)"),
                {"o": str(world.b.org_id)},
            )
            conn.commit()
        with engine.connect() as conn:  # same physical connection, "another request"
            assert _ctx(conn)[1] == str(world.b.org_id)
            assert set(conn.execute(text("SELECT org_id FROM workspaces")).scalars()) == {
                world.b.org_id
            }
            conn.rollback()
            conn.execute(text("RESET ALL"))
            conn.commit()
            assert _ctx(conn) == ("", "", "", "")
    finally:
        engine.dispose()


def test_tenant_session_masks_session_level_user_and_org(app_url: URL, world: World) -> None:
    """A poisoned session-level org/user is overridden by tenant_session's local values,
    including explicit empties (org_id=None)."""
    engine = _single_conn_engine(app_url)
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT set_config('app.current_org_id', :o, false),"
                    " set_config('app.current_user_id', :u, false)"
                ),
                {"o": str(world.b.org_id), "u": str(world.b.owner_id)},
            )
            conn.commit()
        a = world.a
        with tenant_session(user_id=a.owner_id, org_id=a.org_id, engine=engine) as s:
            assert set(s.execute(text("SELECT org_id FROM workspaces")).scalars()) == {a.org_id}
        with tenant_session(user_id=a.owner_id, org_id=None, engine=engine) as s:
            assert s.execute(text("SELECT count(*) FROM workspaces")).scalar() == 0
            assert set(s.execute(text("SELECT id FROM users")).scalars()) == {a.owner_id}
    finally:
        engine.dispose()


# Regression for the former gap fixed in app.core.db.set_tenant_context (clears identity GUCs).
def test_tenant_session_masks_session_level_identity(app_url: URL, world: World) -> None:
    engine = _single_conn_engine(app_url)
    b_owner = world.b.owner_id
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT set_config('app.current_issuer', 'http://issuer.test', false),"
                    " set_config('app.current_subject', :s, false)"
                ),
                {"s": world.subject(b_owner)},
            )
            conn.commit()
        a = world.a
        with tenant_session(user_id=a.owner_id, org_id=a.org_id, engine=engine) as s:
            visible = set(s.execute(text("SELECT id FROM users")).scalars())
        assert b_owner not in visible
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "setting", ["app.current_org_id", "app.current_user_id", "app.current_subject"]
)
def test_app_role_cannot_persist_context_via_alter_role_self(
    app_engine: Engine, admin_engine: Engine, world: World, app_role: str, setting: str
) -> None:
    """A per-role default would inject a context into every NEW app-role connection. On this
    server (PostgreSQL 17) a non-superuser may not set placeholder (custom) GUCs via ALTER ROLE
    SET, even for itself: 42501. Cleanup as admin regardless."""
    try:
        expect_denied(app_engine, f"ALTER ROLE {app_role} SET {setting} = '{world.b.org_id}'")
    finally:
        with admin_engine.begin() as conn:
            conn.execute(text(f"ALTER ROLE {app_role} RESET ALL"))
        with admin_engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM pg_db_role_setting s JOIN pg_roles r"
                        " ON r.oid = s.setrole WHERE r.rolname = :r"
                    ),
                    {"r": app_role},
                ).scalar_one()
                == 0
            )


def test_cannot_set_context_for_other_roles(app_engine: Engine, world: World) -> None:
    expect_denied(
        app_engine,
        f"ALTER ROLE ukos_admin SET app.current_org_id = '{world.b.org_id}'",
    )
    expect_denied(
        app_engine,
        f"ALTER DATABASE ukos SET app.current_org_id = '{world.b.org_id}'",
    )
