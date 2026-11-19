"""Adversarial coverage of the M04 chat and AI-policy tables (migration 0007).

Threads, messages, runs and run events are private to the thread creator: every read needs the org
AND the user context. ``ai_policies`` is org-scoped (any member context reads, only an org
owner/admin writes). Attacks run as BOTH least-privilege roles (``app`` = the API, ``worker``) and
always roll back. The registry (``rls_chat_world``) is cross-checked against the live catalog here
and by test_16's completeness gate.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from rls_chat_world import (
    CASE_IDS,
    CASES,
    CHAT_GRANTS,
    CHAT_TABLES,
    PRIVATE_SELECT_CASES,
    PRIVATE_SELECT_IDS,
    PRIVATE_TABLES,
    ROLES,
    SELECT_CASES,
    SELECT_IDS,
    ChatScope,
    ChatWorld,
    seed_extra_thread,
)
from rls_world import World, admin_count, app_tx, expect_denied, scalar, sqlstate
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

GARBAGE = ["' OR true --", "*", "not-a-uuid", " ", "null", "{}", "x" * 3000, chr(0x202E)]
DENIED = "42501"


def _pk_where(table: str) -> str:
    return {
        "ai_policies": "org_id = :id",
        "chat_threads": "id = :id",
        "chat_messages": "id = :id",
        "chat_runs": "id = :id",
        "chat_run_events": "seq = :id",
    }[table]


def _pk(table: str, s: ChatScope) -> Any:
    return {
        "ai_policies": s.org,
        "chat_threads": s.thread,
        "chat_messages": s.message,
        "chat_runs": s.run,
        "chat_run_events": s.event_seq,
    }[table]


# ------------------------------------------------------------------------------- visibility


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_chat_select_without_context_sees_nothing(
    engines: dict[str, Engine], chat: ChatWorld, role: str, table: str
) -> None:
    if CHAT_GRANTS[role][table].select:
        with app_tx(engines[role]) as conn:  # no set_config at all
            assert scalar(conn, f"SELECT count(*) FROM {table}") == 0
    else:
        expect_denied(engines[role], f"SELECT count(*) FROM {table}", match="permission denied")


@pytest.mark.parametrize(("role", "table"), SELECT_CASES, ids=SELECT_IDS)
def test_chat_select_is_confined_to_the_context_org(
    engines: dict[str, Engine], chat: ChatWorld, role: str, table: str
) -> None:
    a, b = chat.a, chat.b
    with app_tx(engines[role], user=a.creator, org=a.org) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") >= 1
        assert scalar(conn, f"SELECT count(*) FROM {table} WHERE org_id <> :o", {"o": a.org}) == 0
    # The creator's user id in B's org context sees nothing private, and only B's policy row.
    with app_tx(engines[role], user=a.creator, org=b.org) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table} WHERE org_id = :o", {"o": a.org}) == 0
        if table in PRIVATE_TABLES:
            assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize(("role", "table"), SELECT_CASES, ids=SELECT_IDS)
def test_lookup_by_primary_key_of_a_foreign_row_finds_nothing(
    engines: dict[str, Engine], chat: ChatWorld, role: str, table: str
) -> None:
    for mine, theirs in ((chat.a, chat.b), (chat.b, chat.a)):
        with app_tx(engines[role], user=mine.creator, org=mine.org) as conn:
            sql = f"SELECT count(*) FROM {table} WHERE {_pk_where(table)}"
            assert scalar(conn, sql, {"id": _pk(table, theirs)}) == 0
            assert scalar(conn, sql, {"id": _pk(table, mine)}) == 1


@pytest.mark.parametrize(("role", "table"), PRIVATE_SELECT_CASES, ids=PRIVATE_SELECT_IDS)
def test_private_rows_are_invisible_to_everyone_but_the_creator(
    engines: dict[str, Engine], chat: ChatWorld, role: str, table: str
) -> None:
    a, b = chat.a, chat.b
    sql = f"SELECT count(*) FROM {table}"
    contexts: dict[str, dict[str, Any]] = {
        "org owner (not the creator)": {"user": a.other, "org": a.org},
        "org only, no user": {"org": a.org},
        "user only, no org": {"user": a.creator},
        "the other org's creator": {"user": b.creator, "org": a.org},
        "nil user": {"user": uuid.UUID(int=0), "org": a.org},
    }
    for label, ctx in contexts.items():
        with app_tx(engines[role], **ctx) as conn:
            assert scalar(conn, sql) == 0, label
    with app_tx(engines[role], user=a.creator, org=a.org) as conn:
        assert scalar(conn, sql) >= 1


@pytest.mark.parametrize("role", ROLES)
def test_ai_policy_rows_are_readable_in_org_context_only(
    engines: dict[str, Engine], chat: ChatWorld, role: str
) -> None:
    a = chat.a
    for user in (a.creator, a.other, None):
        with app_tx(engines[role], user=user, org=a.org) as conn:
            assert scalar(conn, "SELECT count(*) FROM ai_policies") == 1
    with app_tx(engines[role], user=a.creator) as conn:
        assert scalar(conn, "SELECT count(*) FROM ai_policies") == 0


@pytest.mark.parametrize(("role", "table"), SELECT_CASES, ids=SELECT_IDS)
def test_garbage_context_fails_closed_for_reads(
    engines: dict[str, Engine], chat: ChatWorld, role: str, table: str
) -> None:
    """A malformed org or user GUC is a hard 22P02 or an empty result, never a widened predicate."""
    a = chat.a
    for bad in GARBAGE:
        for key, other_key, other in (
            ("app.current_org_id", "app.current_user_id", a.creator),
            ("app.current_user_id", "app.current_org_id", a.org),
        ):
            with engines[role].connect() as conn, conn.begin():
                conn.execute(text("SELECT set_config(:k, :v, true)"), {"k": key, "v": bad})
                conn.execute(
                    text("SELECT set_config(:k, :v, true)"), {"k": other_key, "v": str(other)}
                )
                try:
                    n = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                except DBAPIError as exc:
                    state = sqlstate(exc)
                    if state != "22P02":  # the only acceptable hard failure
                        pytest.fail(f"{key}={bad[:20]!r}: {state} {exc.orig}")
                else:
                    # ai_policies ignores the user GUC by design (org-only visibility).
                    allowed = 1 if table == "ai_policies" and key == "app.current_user_id" else 0
                    assert n == allowed, f"{role}/{table}: {n} rows for garbage {key}={bad[:20]!r}"


# ------------------------------------------------------------------------- write attacks (INSERT)


def _insert_thread(
    scope: ChatScope, *, org: uuid.UUID, user: uuid.UUID, ws: uuid.UUID | None = None
):
    return (
        "INSERT INTO chat_threads (id, org_id, workspace_id, created_by) VALUES (:i, :o, :w, :u)",
        {"i": uuid.uuid4(), "o": org, "w": ws, "u": user},
    )


def test_app_may_create_only_its_own_thread_in_its_own_org(
    engines: dict[str, Engine], chat: ChatWorld
) -> None:
    a, b = chat.a, chat.b
    with app_tx(engines["app"], user=a.creator, org=a.org) as conn:
        sql, p = _insert_thread(a, org=a.org, user=a.creator, ws=a.ws)
        conn.execute(text(sql), p)  # the positive control
    # Forged creator, forged org, no context at all.
    for ctx, org, user in (
        ({"user": a.creator, "org": a.org}, a.org, a.other),
        ({"user": a.creator, "org": a.org}, b.org, a.creator),
        ({"user": a.creator, "org": a.org}, b.org, b.creator),
        ({"org": a.org}, a.org, a.creator),
        ({"user": a.creator}, a.org, a.creator),
        ({}, a.org, a.creator),
    ):
        sql, p = _insert_thread(a, org=org, user=user)
        expect_denied(engines["app"], sql, p, states=(DENIED,), **ctx)


def test_a_thread_cannot_point_at_another_orgs_workspace(
    engines: dict[str, Engine], chat: ChatWorld
) -> None:
    a, b = chat.a, chat.b
    sql, p = _insert_thread(a, org=a.org, user=a.creator, ws=b.ws)
    expect_denied(engines["app"], sql, p, states=("23503",), user=a.creator, org=a.org)


def _insert_message(
    thread: uuid.UUID, org: uuid.UUID, role: str = "user"
) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO chat_messages (id, org_id, thread_id, role, content)"
        " VALUES (:i, :o, :t, :r, 'forged')",
        {"i": uuid.uuid4(), "o": org, "t": thread, "r": role},
    )


@pytest.mark.parametrize("role", ["app", "worker"])
def test_messages_can_only_be_added_to_the_creators_own_live_thread(
    engines: dict[str, Engine], admin_engine: Engine, chat: ChatWorld, role: str
) -> None:
    a, b = chat.a, chat.b
    msg_role = "user" if role == "app" else "assistant"
    sql, p = _insert_message(a.thread, a.org, msg_role)
    with app_tx(engines[role], user=a.creator, org=a.org) as conn:
        conn.execute(text(sql), p)  # positive control
    for ctx in (
        {"user": a.other, "org": a.org},  # a member who does not own the thread
        {"org": a.org},
        {"user": b.creator, "org": b.org},  # the other tenant
        {"user": a.creator, "org": b.org},
    ):
        expect_denied(
            engines[role], *_insert_message(a.thread, a.org, msg_role), states=(DENIED,), **ctx
        )
    # A forged org_id for the thread of another org.
    expect_denied(
        engines[role],
        *_insert_message(b.thread, a.org, msg_role),
        states=(DENIED,),
        user=a.creator,
        org=a.org,
    )
    deleted = seed_extra_thread(admin_engine, a, deleted=True)
    expect_denied(
        engines[role],
        *_insert_message(deleted, a.org, msg_role),
        states=(DENIED,),
        user=a.creator,
        org=a.org,
    )


def _insert_run(
    thread: uuid.UUID, org: uuid.UUID, user: uuid.UUID, key: str | None = None
) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO chat_runs (id, org_id, thread_id, user_message_id, created_by,"
        " idempotency_key) VALUES (:i, :o, :t, :m, :u, :k)",
        {"i": uuid.uuid4(), "o": org, "t": thread, "m": uuid.uuid4(), "u": user,
         "k": key or uuid.uuid4().hex},
    )  # fmt: skip


def test_runs_can_only_be_created_by_the_thread_creator_in_a_live_thread(
    engines: dict[str, Engine], admin_engine: Engine, chat: ChatWorld
) -> None:
    a, b = chat.a, chat.b
    cases = (
        (
            _insert_run(a.thread, a.org, a.other),
            {"user": a.other, "org": a.org},
        ),  # not their thread
        (
            _insert_run(a.thread, a.org, a.other),
            {"user": a.creator, "org": a.org},
        ),  # forged creator
        (_insert_run(a.thread, b.org, a.creator), {"user": a.creator, "org": a.org}),  # forged org
        (
            _insert_run(b.thread, a.org, a.creator),
            {"user": a.creator, "org": a.org},
        ),  # foreign thread
        (_insert_run(a.thread, a.org, a.creator), {"org": a.org}),
        (_insert_run(a.thread, a.org, a.creator), {"user": b.creator, "org": b.org}),
    )
    for (sql, params), ctx in cases:
        expect_denied(engines["app"], sql, params, states=(DENIED,), **ctx)
    deleted = seed_extra_thread(admin_engine, a, deleted=True)
    expect_denied(
        engines["app"],
        *_insert_run(deleted, a.org, a.creator),
        states=(DENIED,),
        user=a.creator,
        org=a.org,
    )
    # The worker has no INSERT on runs or threads at all.
    expect_denied(
        engines["worker"],
        *_insert_run(a.thread, a.org, a.creator),
        match="permission denied",
        user=a.creator,
        org=a.org,
    )
    expect_denied(
        engines["worker"],
        *_insert_thread(a, org=a.org, user=a.creator),
        match="permission denied",
        user=a.creator,
        org=a.org,
    )


def test_on_conflict_cannot_be_used_to_rewrite_a_run(
    engines: dict[str, Engine], chat: ChatWorld
) -> None:
    a = chat.a
    sql, p = _insert_run(a.thread, a.org, a.creator)
    p["k"] = f"key-{a.run.hex[:8]}"  # the seeded run's own idempotency key
    attack = (
        sql + " ON CONFLICT ON CONSTRAINT uq_chat_runs_thread_id_idempotency_key"
        " DO UPDATE SET state = 'completed', created_by = EXCLUDED.created_by"
    )
    expect_denied(engines["app"], attack, p, states=(DENIED,), user=a.creator, org=a.org)
    # DO NOTHING on the creator's own key is harmless and writes nothing.
    with app_tx(engines["app"], user=a.creator, org=a.org) as conn:
        r = conn.execute(
            text(
                sql + " ON CONFLICT ON CONSTRAINT uq_chat_runs_thread_id_idempotency_key DO NOTHING"
            ),
            p,
        )
        assert r.rowcount == 0


def test_events_come_from_the_worker_acting_for_the_run_creator_only(
    engines: dict[str, Engine], chat: ChatWorld
) -> None:
    a, b = chat.a, chat.b
    sql = "INSERT INTO chat_run_events (run_id, org_id, type) VALUES (:r, :o, 'delta')"
    with app_tx(engines["worker"], user=a.creator, org=a.org) as conn:
        conn.execute(text(sql), {"r": a.run, "o": a.org})  # positive control
    for ctx, params in (
        ({"user": a.other, "org": a.org}, {"r": a.run, "o": a.org}),  # not the creator
        ({"org": a.org}, {"r": a.run, "o": a.org}),
        ({"user": a.creator, "org": a.org}, {"r": b.run, "o": b.org}),  # forged org (B's run)
        ({"user": a.creator, "org": a.org}, {"r": b.run, "o": a.org}),
        ({"user": b.creator, "org": b.org}, {"r": a.run, "o": a.org}),
    ):
        expect_denied(engines["worker"], sql, params, states=(DENIED,), **ctx)
    # The API role cannot write events at all.
    expect_denied(
        engines["app"], sql, {"r": a.run, "o": a.org}, match="permission denied",
        user=a.creator, org=a.org,
    )  # fmt: skip


@pytest.mark.parametrize("role", ROLES)
def test_ai_policy_insert_needs_an_org_admin_in_the_org(
    engines: dict[str, Engine], admin_engine: Engine, chat: ChatWorld, role: str
) -> None:
    a = chat.a
    sql = "INSERT INTO ai_policies (org_id, updated_by) VALUES (:o, :u)"
    with admin_engine.begin() as conn:  # a fresh org without a policy row
        org = uuid.uuid4()
        conn.execute(
            text("INSERT INTO organizations (id, slug, name, created_by) VALUES (:o, :s, 'x', :u)"),
            {"o": org, "s": f"p-{org.hex[:10]}", "u": a.creator},
        )
        for user, r in ((a.creator, "owner"), (a.other, "member")):
            conn.execute(
                text(
                    "INSERT INTO organization_memberships (org_id, user_id, role)"
                    " VALUES (:o, :u, :r)"
                ),
                {"o": org, "u": user, "r": r},
            )
    try:
        if role == "worker":
            expect_denied(engines[role], sql, {"o": org, "u": a.creator}, match="permission denied",
                          user=a.creator, org=org)  # fmt: skip
            return
        # a plain member and a stranger are refused, the owner may write
        expect_denied(
            engines[role], sql, {"o": org, "u": a.other}, states=(DENIED,), user=a.other, org=org
        )
        expect_denied(engines[role], sql, {"o": org, "u": a.other}, states=(DENIED,),
                      user=chat.b.creator, org=org)  # fmt: skip
        expect_denied(engines[role], sql, {"o": org, "u": a.creator}, states=(DENIED,), org=org)
        with app_tx(engines[role], user=a.creator, org=org) as conn:
            conn.execute(text(sql), {"o": org, "u": a.creator})
        # The admin of org A cannot write a policy row for another org (context decides).
        expect_denied(
            engines[role], sql, {"o": chat.b.org, "u": a.creator}, states=(DENIED,),
            user=a.creator, org=a.org,
        )  # fmt: skip
    finally:
        with admin_engine.begin() as conn:
            conn.execute(text("DELETE FROM ai_policies WHERE org_id = :o"), {"o": org})
            conn.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": org})


# ------------------------------------------------------------------------ write attacks (UPDATE)


def test_app_updates_threads_only_in_the_granted_columns_and_only_its_own(
    engines: dict[str, Engine], chat: ChatWorld
) -> None:
    a, b = chat.a, chat.b
    with app_tx(engines["app"], user=a.creator, org=a.org) as conn:
        r = conn.execute(
            text("UPDATE chat_threads SET title = 'renamed', deleted_at = now() WHERE id = :t"),
            {"t": a.thread},
        )
        assert r.rowcount == 1
    for ctx, tid in (
        ({"user": a.other, "org": a.org}, a.thread),
        ({"user": b.creator, "org": b.org}, a.thread),
        ({"org": a.org}, a.thread),
    ):
        with app_tx(engines["app"], **ctx) as conn:
            r = conn.execute(
                text("UPDATE chat_threads SET title = 'pwned' WHERE id = :t"), {"t": tid}
            )
            assert r.rowcount == 0, ctx
    for column, value in (
        ("created_by", ":other"),
        ("org_id", ":other_org"),
        ("workspace_id", "NULL"),
        ("id", "gen_random_uuid()"),
        ("created_at", "now()"),
    ):
        expect_denied(
            engines["app"],
            f"UPDATE chat_threads SET {column} = {value} WHERE id = :t",
            {"t": a.thread, "other": a.other, "other_org": b.org},
            match="permission denied",
            user=a.creator,
            org=a.org,
        )


def test_a_thread_cannot_be_handed_to_another_user_or_org_by_update(
    engines: dict[str, Engine], chat: ChatWorld
) -> None:
    a = chat.a
    # Even a legal column update cannot smuggle the row out of the creator's policy: WITH CHECK.
    with app_tx(engines["app"], user=a.creator, org=a.org) as conn:
        conn.execute(text("UPDATE chat_threads SET title = 'x' WHERE id = :t"), {"t": a.thread})
        assert (
            scalar(conn, "SELECT created_by FROM chat_threads WHERE id = :t", {"t": a.thread})
            == a.creator
        )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE chat_runs SET state = 'completed'",
        "UPDATE chat_runs SET error_code = 'x'",
        "UPDATE chat_messages SET content = 'rewritten'",
        "UPDATE chat_run_events SET type = 'completed'",
    ],
)
def test_app_has_no_update_on_runs_messages_or_events(
    engines: dict[str, Engine], chat: ChatWorld, sql: str
) -> None:
    expect_denied(
        engines["app"], sql, match="permission denied", user=chat.a.creator, org=chat.a.org
    )


@pytest.mark.parametrize(
    "column",
    [
        "attempts",
        "lease_epoch",
        "created_by",
        "org_id",
        "scope_snapshot",
        "idempotency_key",
        "thread_id",
    ],
)
def test_worker_cannot_touch_identity_scope_or_fencing_columns_of_a_run(
    engines: dict[str, Engine], chat: ChatWorld, column: str
) -> None:
    a = chat.a
    value = {
        "attempts": "99",
        "lease_epoch": "99",
        "created_by": ":other",
        "org_id": ":other",
        "scope_snapshot": "'{}'::jsonb",
        "idempotency_key": "'k'",
        "thread_id": ":other",
    }[column]
    expect_denied(
        engines["worker"],
        f"UPDATE chat_runs SET {column} = {value} WHERE id = :r",
        {"r": a.run, "other": a.other},
        match="permission denied",
        user=a.creator,
        org=a.org,
    )


def test_worker_run_updates_are_confined_to_the_run_creators_context(
    engines: dict[str, Engine], chat: ChatWorld
) -> None:
    a, b = chat.a, chat.b
    sql = "UPDATE chat_runs SET lease_expires_at = now() WHERE id = :r"
    with app_tx(engines["worker"], user=a.creator, org=a.org) as conn:
        assert conn.execute(text(sql), {"r": a.run}).rowcount == 1
    for ctx in (
        {"user": a.other, "org": a.org},
        {"org": a.org},
        {"user": b.creator, "org": b.org},
        {"user": a.creator, "org": b.org},
    ):
        with app_tx(engines["worker"], **ctx) as conn:
            assert conn.execute(text(sql), {"r": a.run}).rowcount == 0, ctx
    with app_tx(engines["worker"], user=a.creator, org=a.org) as conn:
        assert (
            conn.execute(
                text("UPDATE chat_threads SET updated_at = now() WHERE id = :t"), {"t": a.thread}
            ).rowcount
            == 1
        )
    expect_denied(
        engines["worker"], "UPDATE chat_threads SET title = 'x'", match="permission denied",
        user=a.creator, org=a.org,
    )  # fmt: skip


def test_ai_policy_update_is_for_org_admins_and_never_moves_the_row(
    engines: dict[str, Engine], chat: ChatWorld
) -> None:
    a, b = chat.a, chat.b
    sql = "UPDATE ai_policies SET allow_external = true, allowed_providers = '{local,openai}'"
    for user in (a.creator, b.creator, None):  # a plain member, a stranger, nobody
        with app_tx(engines["app"], user=user, org=a.org) as conn:
            assert conn.execute(text(sql)).rowcount == 0, user
    with app_tx(engines["app"], user=a.other, org=a.org) as conn:  # the org owner
        assert conn.execute(text(sql)).rowcount == 1
    with app_tx(engines["app"], user=a.other, org=b.org) as conn:  # owner of A, context B
        assert conn.execute(text(sql)).rowcount == 0
    expect_denied(
        engines["app"], "UPDATE ai_policies SET org_id = :o", {"o": b.org},
        match="permission denied", user=a.other, org=a.org,
    )  # fmt: skip
    expect_denied(engines["worker"], sql, match="permission denied", user=a.other, org=a.org)


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_nobody_can_delete_or_truncate(
    engines: dict[str, Engine], chat: ChatWorld, role: str, table: str
) -> None:
    a = chat.a
    for sql in (f"DELETE FROM {table}", f"TRUNCATE {table}"):
        expect_denied(engines[role], sql, match="permission denied", user=a.creator, org=a.org)


# ------------------------------------------------------------------------------ definer functions


def _rows(conn: Any, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def test_cancel_run_acts_only_for_the_creator_in_the_org(
    engines: dict[str, Engine], admin_engine: Engine, chat: ChatWorld
) -> None:
    a, b = chat.a, chat.b
    sql = "SELECT * FROM chat_cancel_run(:r)"
    for label, ctx in (
        ("org owner, not creator", {"user": a.other, "org": a.org}),
        ("no user", {"org": a.org}),
        ("no org", {"user": a.creator}),
        ("no context", {}),
        ("creator in the wrong org", {"user": a.creator, "org": b.org}),
        ("other tenant's creator", {"user": b.creator, "org": b.org}),
        ("other tenant's creator, org A", {"user": b.creator, "org": a.org}),
        ("nil uuids", {"user": uuid.UUID(int=0), "org": uuid.UUID(int=0)}),
    ):
        with app_tx(engines["app"], **ctx) as conn:
            assert _rows(conn, sql, r=a.run) == [], label
    with app_tx(engines["app"], user=a.creator, org=a.org) as conn:
        assert _rows(conn, sql, r=uuid.uuid4()) == []  # unknown run
        assert _rows(conn, sql, r=None) == []
        first = _rows(conn, sql, r=a.run)
        again = _rows(conn, sql, r=a.run)
        assert [(x["state"], x["cancelled"]) for x in first] == [("cancelled", True)]
        assert [(x["state"], x["cancelled"]) for x in again] == [("cancelled", False)]
    assert (
        admin_count(
            admin_engine,
            "SELECT count(*) FROM chat_runs WHERE id = :r AND state = 'queued'",
            {"r": a.run},
        )
        == 1
    )
    with app_tx(engines["app"], user=a.creator, org=a.org) as conn:
        assert _rows(conn, sql, r=b.run) == []  # another tenant's run id


def test_definers_ignore_forged_arguments_and_shadow_tables(
    engines: dict[str, Engine], chat: ChatWorld
) -> None:
    """Relations inside the definers are schema-qualified: empty TEMP tables of the same name
    placed first in the caller's own search path change nothing."""
    a = chat.a
    shadows = (
        "CREATE TEMP TABLE chat_runs (id uuid, org_id uuid, state text, created_by uuid)",
        "CREATE TEMP TABLE chat_run_events (run_id uuid, org_id uuid, type text)",
        "CREATE TEMP TABLE organizations (id uuid, deleted_at timestamptz)",
        "CREATE TEMP TABLE workspaces (id uuid, org_id uuid, deleted_at timestamptz)",
    )
    with app_tx(engines["app"], user=a.creator, org=a.org) as conn:
        for ddl in shadows:
            conn.execute(text(ddl))
        conn.execute(text("SET LOCAL search_path = pg_temp, public"))
        rows = _rows(conn, "SELECT * FROM public.chat_cancel_run(:r)", r=a.run)
        assert [(r["state"], r["cancelled"]) for r in rows] == [("cancelled", True)]
        assert (
            scalar(conn, "SELECT state FROM public.chat_runs WHERE id = :r", {"r": a.run})
            == "cancelled"
        )
    with app_tx(engines["worker"], user=a.creator, org=a.org) as conn:
        for ddl in shadows:
            conn.execute(text(ddl))
        conn.execute(text("SET LOCAL search_path = pg_temp, public"))
        claimed = _rows(conn, "SELECT * FROM public.chat_claim_run(:r, 'w', 30)", r=a.run)
        assert len(claimed) == 1
        assert claimed[0]["lease_epoch"] == 1
        assert (
            scalar(conn, "SELECT state FROM public.chat_runs WHERE id = :r", {"r": a.run})
            == "running"
        )
        readable = _rows(
            conn, "SELECT * FROM public.chat_readable_workspaces(:o, :u)", o=a.org, u=a.other
        )
        assert {r["workspace_id"] for r in readable} == {a.ws}


def test_claim_returns_one_row_only_for_a_queued_run_and_validates_arguments(
    engines: dict[str, Engine], chat: ChatWorld
) -> None:
    a = chat.a
    with app_tx(engines["worker"]) as conn:  # no context needed: the definer looks across orgs
        first = _rows(conn, "SELECT * FROM chat_claim_run(:r, 'w1', 30)", r=a.run)
        assert len(first) == 1
        row = first[0]
        assert (row["run_id"], row["org_id"], row["thread_id"], row["created_by"]) == (
            a.run, a.org, a.thread, a.creator,
        )  # fmt: skip
        assert (row["attempts"], row["lease_epoch"], row["user_message_id"]) == (1, 1, a.message)
        assert _rows(conn, "SELECT * FROM chat_claim_run(:r, 'w2', 30)", r=a.run) == []
        assert _rows(conn, "SELECT * FROM chat_claim_run(:r, 'w2', 30)", r=uuid.uuid4()) == []
    for args in ("NULL, 30", "'', 30", "'w', 0", "'w', NULL", "'w', -5"):
        expect_denied(
            engines["worker"], f"SELECT * FROM chat_claim_run(:r, {args})", {"r": a.run},
            states=("22023",),
        )  # fmt: skip
    expect_denied(engines["worker"], "SELECT * FROM chat_reap_expired_runs(0)", states=("22023",))
    expect_denied(
        engines["worker"], "SELECT * FROM chat_reap_expired_runs(NULL)", states=("22023",)
    )


def test_readable_workspaces_never_crosses_tenants(
    engines: dict[str, Engine], chat: ChatWorld
) -> None:
    a, b = chat.a, chat.b
    with app_tx(engines["worker"]) as conn:
        q = "SELECT workspace_id FROM chat_readable_workspaces(:o, :u)"
        assert {r["workspace_id"] for r in _rows(conn, q, o=a.org, u=a.other)} == {a.ws}
        assert _rows(conn, q, o=a.org, u=b.other) == []  # B's owner is nothing in org A
        assert _rows(conn, q, o=b.org, u=a.other) == []
        assert _rows(conn, q, o=None, u=a.other) == []
        assert _rows(conn, q, o=a.org, u=None) == []
        assert _rows(conn, q, o=uuid.uuid4(), u=a.other) == []


def test_definers_are_not_callable_across_roles(
    engines: dict[str, Engine], chat: ChatWorld
) -> None:
    a = chat.a
    for sql in (
        "SELECT * FROM chat_claim_run(gen_random_uuid(), 'w', 30)",
        "SELECT * FROM chat_reap_expired_runs(2)",
        "SELECT * FROM chat_readable_workspaces(gen_random_uuid(), gen_random_uuid())",
    ):
        expect_denied(engines["app"], sql, match="permission denied", user=a.creator, org=a.org)
    expect_denied(
        engines["worker"], "SELECT * FROM chat_cancel_run(gen_random_uuid())",
        match="permission denied", user=a.creator, org=a.org,
    )  # fmt: skip


def test_reaper_leaves_live_leases_alone(engines: dict[str, Engine], chat: ChatWorld) -> None:
    with app_tx(engines["worker"]) as conn:
        # The seeded runs are queued; a freshly claimed run has a live lease: neither is reaped.
        _rows(conn, "SELECT * FROM chat_claim_run(:r, 'w', 600)", r=chat.a.run)
        assert _rows(conn, "SELECT * FROM chat_reap_expired_runs(2)") == []


# -------------------------------------------------------------------- catalog vs registry


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_grants_match_the_registry(
    admin_engine: Engine, role_names: dict[str, str], migrated: None, role: str, table: str
) -> None:
    grant = CHAT_GRANTS[role][table]
    name = role_names[role]
    with admin_engine.connect() as conn:

        def has(priv: str) -> bool:
            return bool(
                conn.execute(
                    text("SELECT has_table_privilege(:r, CAST(:t AS regclass), :p)"),
                    {"r": name, "t": f"public.{table}", "p": priv},
                ).scalar_one()
            )

        assert has("SELECT") == grant.select
        assert has("INSERT") == grant.insert
        for forbidden in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert not has(forbidden), forbidden
        columns = conn.execute(
            text("SELECT column_name FROM information_schema.columns"
                 " WHERE table_schema = 'public' AND table_name = :t"),
            {"t": table},
        ).scalars()  # fmt: skip
        updatable = {
            c
            for c in columns
            if conn.execute(
                text("SELECT has_column_privilege(:r, CAST(:t AS regclass), :c, 'UPDATE')"),
                {"r": name, "t": f"public.{table}", "c": c},
            ).scalar_one()
        }
    assert updatable == set(grant.update)


@pytest.mark.parametrize("table", CHAT_TABLES)
def test_rls_is_forced_with_per_command_policies_binding_every_role(
    admin_engine: Engine, migrated: None, table: str
) -> None:
    expected = {
        "ai_policies": {"SELECT", "INSERT", "UPDATE"},
        "chat_threads": {"SELECT", "INSERT", "UPDATE"},
        "chat_messages": {"SELECT", "INSERT"},
        "chat_runs": {"SELECT", "INSERT", "UPDATE"},
        "chat_run_events": {"SELECT", "INSERT"},
    }[table]
    with admin_engine.connect() as conn:
        flags = conn.execute(
            text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"),
            {"t": table},
        ).one()
        policies = conn.execute(
            text("SELECT cmd, roles::text FROM pg_policies WHERE tablename = :t"), {"t": table}
        ).all()
    assert tuple(flags) == (True, True)
    assert {p.cmd for p in policies} == expected
    assert len(policies) == len(expected)
    assert all(p.roles == "{public}" for p in policies)  # no TO clause: every role is bound
    # Every policy is org-keyed (no USING (true) anywhere).
    with admin_engine.connect() as conn:
        quals = conn.execute(
            text("SELECT coalesce(qual, '') || coalesce(with_check, '') FROM pg_policies"
                 " WHERE tablename = :t"),
            {"t": table},
        ).scalars()  # fmt: skip
        assert all("app.current_org_id" in q for q in quals)
    if table in PRIVATE_TABLES and table in ("chat_threads", "chat_runs"):
        with admin_engine.connect() as conn:
            quals = list(
                conn.execute(
                    text(
                        "SELECT coalesce(qual, '') FROM pg_policies"
                        " WHERE tablename = :t AND cmd = 'SELECT'"
                    ),
                    {"t": table},
                ).scalars()
            )
        assert all("app.current_user_id" in q for q in quals)


def test_world_is_wired(chat: ChatWorld, world: World) -> None:
    assert chat.a.org == world.a.org_id
    assert chat.b.org == world.b.org_id
    assert chat.a.creator != chat.a.other
    assert set(ROLES) == {"app", "worker"}
    assert set(CHAT_GRANTS) == set(ROLES)
