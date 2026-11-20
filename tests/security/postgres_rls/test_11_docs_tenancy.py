"""Tenant isolation of the M02 + M03 tables for BOTH least-privilege roles (app and worker).

Every test is parametrised over ``(role, table)`` from the registry in ``rls_docs_world`` (which
test_10 cross-checks against the catalog); a table without an entry fails test_15.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from rls_docs_world import (
    BUILDERS,
    CASE_IDS,
    CASES,
    CONFLICT_CASES,
    DOC_TABLES,
    GRANTS,
    INSERT_CASES,
    ORACLE_CASES,
    PARENTS,
    PK_CONFLICT,
    ROLES,
    SELECT_CASES,
    UPDATE_CASES,
    UUID_RE,
    DocWorld,
    admin_tx,
    become,
    ids,
    pk_params,
    pk_where,
)
from rls_world import World, admin_count, app_tx, expect_denied, scalar, set_ctx, sqlstate
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

GARBAGE = [
    "' OR true --",
    "*",
    "%",
    "not-a-uuid",
    " ",
    "null",
    "NULL",
    "0",
    "{}",
    "00000000-0000-0000-0000-00000000000g",
    "'; DROP TABLE documents; --",
    chr(0x202E),  # right-to-left override
    "é" * 40,
    "x" * 5000,
]


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_select_without_context_sees_nothing(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    if GRANTS[role][table].select:
        with app_tx(engines[role]) as conn:  # no set_config at all
            assert scalar(conn, f"SELECT count(*) FROM {table}") == 0
    else:
        expect_denied(engines[role], f"SELECT count(*) FROM {table}", match="permission denied")


@pytest.mark.parametrize(("role", "table"), SELECT_CASES, ids=ids(SELECT_CASES))
def test_select_with_empty_context_sees_nothing(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    with app_tx(engines[role], org=None, user=None) as conn:
        set_ctx(conn)  # explicit empty strings, as app.core.db does for org_id=None
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize(("role", "table"), SELECT_CASES, ids=ids(SELECT_CASES))
def test_nil_uuid_context_sees_nothing(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    with app_tx(engines[role], org=uuid.UUID(int=0)) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize(("role", "table"), SELECT_CASES, ids=ids(SELECT_CASES))
def test_garbage_context_fails_closed_for_reads(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    """A malformed org GUC is a hard 22P02 (nothing returned, nothing leaked) or an empty
    result, never a widened predicate."""
    for bad in GARBAGE:
        with engines[role].connect() as conn, conn.begin():
            conn.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": bad})
            try:
                n = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            except DBAPIError as exc:
                assert sqlstate(exc) == "22P02", (bad[:20], sqlstate(exc), exc.orig)
            else:
                assert n == 0, f"{role}/{table}: {n} rows for garbage context {bad[:20]!r}"


@pytest.mark.parametrize(("role", "table"), INSERT_CASES, ids=ids(INSERT_CASES))
def test_garbage_context_never_allows_writes(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    sql, params = BUILDERS[table](docs.a.org, docs.a.owner, docs.a.ids())
    for bad in GARBAGE:
        with engines[role].connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": bad})
                with pytest.raises(DBAPIError) as info:
                    conn.execute(text(sql), params)
                assert sqlstate(info.value) in ("22P02", "42501"), (bad[:20], info.value.orig)
            finally:
                trans.rollback()


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_insert_without_context_rejected(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    sql, params = BUILDERS[table](docs.a.org, docs.a.owner, docs.a.ids())
    match = "row-level security" if GRANTS[role][table].insert else "permission denied"
    expect_denied(engines[role], sql, params, match=match)


@pytest.mark.parametrize(("role", "table"), UPDATE_CASES, ids=ids(UPDATE_CASES))
def test_update_without_context_affects_nothing(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    cols = sorted(GRANTS[role][table].update)
    with app_tx(engines[role]) as conn:
        assert conn.execute(text(f"UPDATE {table} SET {cols[0]} = {cols[0]}")).rowcount == 0


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_delete_and_truncate_are_denied_with_and_without_context(
    engines: dict[str, Engine], admin_engine: Engine, docs: DocWorld, role: str, table: str
) -> None:
    before = admin_count(admin_engine, f"SELECT count(*) FROM {table}")
    contexts: tuple[dict[str, Any], ...] = ({}, {"org": docs.a.org})
    for ctx in contexts:
        expect_denied(engines[role], f"DELETE FROM {table}", match="permission denied", **ctx)
        expect_denied(engines[role], f"TRUNCATE {table}", match="permission denied", **ctx)
    assert admin_count(admin_engine, f"SELECT count(*) FROM {table}") == before


# -- org A cannot see or touch org B -----------------------------------------------------------


@pytest.mark.parametrize(("role", "table"), SELECT_CASES, ids=ids(SELECT_CASES))
def test_select_is_confined_to_the_context_org(
    engines: dict[str, Engine], admin_engine: Engine, docs: DocWorld, role: str, table: str
) -> None:
    for mine, theirs in ((docs.a, docs.b), (docs.b, docs.a)):
        expected = admin_count(
            admin_engine, f"SELECT count(*) FROM {table} WHERE org_id = :o", {"o": mine.org}
        )
        assert expected >= 1, f"seed has no {table} rows for {mine.org}"
        with app_tx(engines[role], org=mine.org) as conn:
            orgs = set(conn.execute(text(f"SELECT org_id FROM {table}")).scalars())
            assert orgs == {mine.org}
            assert scalar(conn, f"SELECT count(*) FROM {table}") == expected
            # explicit predicate for the other org: nothing, not even through the planner
            assert (
                scalar(conn, f"SELECT count(*) FROM {table} WHERE org_id = :o", {"o": theirs.org})
                == 0
            )
            # primary-key lookup of the other tenant's row
            assert (
                scalar(
                    conn,
                    f"SELECT count(*) FROM {table} WHERE {pk_where(table)}",
                    pk_params(theirs.pk(table)),
                )
                == 0
            )
            # ... while the same lookup of one's own row works (the attack is not vacuous)
            assert (
                scalar(
                    conn,
                    f"SELECT count(*) FROM {table} WHERE {pk_where(table)}",
                    pk_params(mine.pk(table)),
                )
                == 1
            )


@pytest.mark.parametrize(("role", "table"), SELECT_CASES, ids=ids(SELECT_CASES))
def test_aggregate_and_join_do_not_leak_other_org(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    """Aggregates / self-joins / EXISTS subqueries only ever see the context org's rows."""
    with app_tx(engines[role], org=docs.a.org) as conn:
        assert scalar(conn, f"SELECT count(DISTINCT org_id) FROM {table}") == 1
        assert (
            scalar(
                conn,
                f"SELECT count(*) FROM {table} x JOIN {table} y ON x.org_id = y.org_id"
                " WHERE x.org_id = :b",
                {"b": docs.b.org},
            )
            == 0
        )
        assert (
            scalar(
                conn,
                f"SELECT EXISTS (SELECT 1 FROM {table} WHERE org_id = :b)",
                {"b": docs.b.org},
            )
            is False
        )


@pytest.mark.parametrize(("role", "table"), UPDATE_CASES, ids=ids(UPDATE_CASES))
def test_update_cannot_touch_other_org_and_only_own_rows(
    engines: dict[str, Engine], admin_engine: Engine, docs: DocWorld, role: str, table: str
) -> None:
    cols = sorted(GRANTS[role][table].update)
    col = cols[0]
    own = admin_count(
        admin_engine, f"SELECT count(*) FROM {table} WHERE org_id = :o", {"o": docs.a.org}
    )
    with app_tx(engines[role], org=docs.a.org) as conn:
        # B's row by primary key
        hit = conn.execute(
            text(f"UPDATE {table} SET {col} = {col} WHERE {pk_where(table)}"),
            pk_params(docs.b.pk(table)),
        )
        assert hit.rowcount == 0
        # control: A's own row
        mine = conn.execute(
            text(f"UPDATE {table} SET {col} = {col} WHERE {pk_where(table)}"),
            pk_params(docs.a.pk(table)),
        )
        assert mine.rowcount == 1
        # unqualified UPDATE touches exactly A's rows
        assert conn.execute(text(f"UPDATE {table} SET {col} = {col}")).rowcount == own
        # a predicate naming B's org still touches nothing
        assert (
            conn.execute(
                text(f"UPDATE {table} SET {col} = {col} WHERE org_id = :b"),
                {"b": docs.b.org},
            ).rowcount
            == 0
        )


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_update_cannot_move_a_row_to_another_org(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    """org_id (and workspace_id) are not UPDATE-granted to anyone: rejected as a privilege error
    before RLS even runs. (The WITH CHECK of the UPDATE policy is proven by test_12's probe.)"""
    for column in ("org_id", "workspace_id"):
        if table in ("ingestion_checkpoints",) and column == "workspace_id":
            continue  # column does not exist
        if table == "outbox_events" and column == "workspace_id":
            continue
        value = docs.b.org if column == "org_id" else docs.b.ws
        expect_denied(
            engines[role],
            f"UPDATE {table} SET {column} = :v",
            {"v": value},
            match="permission denied",
            org=docs.a.org,
        )


@pytest.mark.parametrize(("role", "table"), INSERT_CASES, ids=ids(INSERT_CASES))
def test_insert_for_own_org_works_positive_control(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    sql, params = BUILDERS[table](docs.a.org, docs.a.owner, docs.a.ids())
    with app_tx(engines[role], org=docs.a.org) as conn:
        assert conn.execute(text(sql), params).rowcount == 1


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
@pytest.mark.parametrize("parents", ["own", "foreign"])
def test_insert_with_other_orgs_org_id_rejected_by_with_check(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str, parents: str
) -> None:
    """Context A, row stamped org B: the policy's WITH CHECK rejects it, whether the parents
    named are A's (own) or B's (foreign)."""
    p = (docs.a if parents == "own" else docs.b).ids()
    sql, params = BUILDERS[table](docs.b.org, docs.b.owner, p)
    match = "row-level security" if GRANTS[role][table].insert else "permission denied"
    expect_denied(engines[role], sql, params, match=match, org=docs.a.org)


def _fk_cases() -> list[tuple[str, str, str]]:
    return [(r, t, k) for r, t in INSERT_CASES for k in PARENTS[t]]


@pytest.mark.parametrize(
    ("role", "table", "key"),
    _fk_cases(),
    ids=[f"{r}-{t}-{k}" for r, t, k in _fk_cases()],
)
def test_composite_fk_pins_every_parent_to_the_same_org_as_the_role(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str, key: str
) -> None:
    """Context A, row stamped org A, but ONE parent reference points into org B's tree: the
    composite foreign keys reject it (23503). RLS alone would not (org_id is A, which passes)."""
    parent_ids = {**docs.a.ids(), key: docs.b.ids()[key]}
    sql, params = BUILDERS[table](docs.a.org, docs.a.owner, parent_ids)
    expect_denied(engines[role], sql, params, states=("23503",), org=docs.a.org)


@pytest.mark.parametrize(
    ("table", "key"),
    [(t, k) for t in DOC_TABLES for k in PARENTS[t]],
    ids=[f"{t}-{k}" for t in DOC_TABLES for k in PARENTS[t]],
)
def test_composite_fk_pins_parent_even_for_the_unrestricted_owner(
    admin_engine: Engine, docs: DocWorld, table: str, key: str
) -> None:
    """The admin bypasses RLS but not foreign keys: a cross-org parent is rejected for it too."""
    parent_ids = {**docs.a.ids(), key: docs.b.ids()[key]}
    sql, params = BUILDERS[table](docs.a.org, docs.a.owner, parent_ids)
    with admin_tx(admin_engine) as conn, pytest.raises(DBAPIError) as info:
        conn.execute(text(sql), params)
    assert sqlstate(info.value) == "23503", info.value.orig


def _complement_cases() -> list[tuple[str, str, str]]:
    """(actor, table, key): the table has more than one parent, so a coherent foreign subtree
    exists. Roles only where they hold INSERT; the admin (no RLS, still bound by FKs) for all."""
    return [
        (actor, t, k)
        for actor in ("app", "worker", "admin")
        for t in DOC_TABLES
        if len(PARENTS[t]) > 1 and (actor == "admin" or GRANTS[actor][t].insert)
        for k in PARENTS[t]
    ]


@pytest.mark.parametrize(
    ("actor", "table", "key"),
    _complement_cases(),
    ids=[f"{a}-{t}-{k}" for a, t, k in _complement_cases()],
)
def test_composite_fk_pins_each_link_individually_in_a_coherent_foreign_subtree(
    engines: dict[str, Engine],
    admin_engine: Engine,
    docs: DocWorld,
    actor: str,
    table: str,
    key: str,
) -> None:
    """Every parent comes from org B EXCEPT ``key`` (org A's). All the other links agree with
    each other, so only the one composite FK that ties ``key`` to its siblings can reject the row.
    Dropping any single pinning constraint therefore lets this insert through (survived-mutation
    regression: removing ``fk_ingestion_jobs_document`` was invisible to single-key swaps)."""
    parent_ids = {**docs.b.ids(), key: docs.a.ids()[key]}
    sql, params = BUILDERS[table](docs.a.org, docs.a.owner, parent_ids)
    if actor == "admin":
        with admin_tx(admin_engine) as conn, pytest.raises(DBAPIError) as info:
            conn.execute(text(sql), params)
        assert sqlstate(info.value) == "23503", info.value.orig
    else:
        expect_denied(engines[actor], sql, params, states=("23503",), org=docs.a.org)


@pytest.mark.parametrize(("role", "table"), CONFLICT_CASES, ids=ids(CONFLICT_CASES))
def test_primary_key_collision_with_other_org_is_an_existence_oracle_documented(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    """DOCUMENTED (informational, low): primary/unique keys are global, so inserting a row from
    org A whose client-chosen key equals one of org B's rows fails with 23505 (duplicate key)
    rather than an RLS error. A caller who already holds a guessed UUID can confirm it exists in
    some other org; the 122-bit random ids and the absence of any row content in the error keep
    this a confirmation oracle only. The error discloses nothing else."""
    pk = PK_CONFLICT[table]
    assert pk is not None
    sql, params = BUILDERS[table](docs.a.org, docs.a.owner, docs.a.ids())
    victim = docs.b.pk(table)
    for column in pk:
        sql, params = _force_key(sql, params, table, column, victim[column])
    err = expect_denied(engines[role], sql, params, states=("23505",), org=docs.a.org)
    assert docs.b.title_marker not in str(err.orig)
    assert str(docs.b.org) not in str(err.orig)


@pytest.mark.parametrize(("role", "table"), ORACLE_CASES, ids=ids(ORACLE_CASES))
def test_fk_error_is_not_an_existence_oracle_for_other_orgs_ids(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    """Referencing a REAL id of org B or a random id fails with the same message: the error
    discloses only what the caller supplied."""
    key = PARENTS[table][0]
    messages = []
    for value in (docs.b.ids()[key], uuid.uuid4()):
        parent_ids = {**docs.a.ids(), key: value}
        sql, params = BUILDERS[table](docs.a.org, docs.a.owner, parent_ids)
        err = expect_denied(engines[role], sql, params, states=("23503",), org=docs.a.org)
        messages.append(re.sub(UUID_RE, "<uuid>", str(err.orig)))
    assert messages[0] == messages[1]


@pytest.mark.parametrize(("role", "table"), CONFLICT_CASES, ids=ids(CONFLICT_CASES))
def test_on_conflict_do_update_cannot_overwrite_other_orgs_row(
    engines: dict[str, Engine], admin_engine: Engine, docs: DocWorld, role: str, table: str
) -> None:
    """An upsert whose key collides with B's row, issued from A's context, must not modify B's
    row (RLS applies to the existing row: error, or DO NOTHING semantics, never an update)."""
    grant = GRANTS[role][table]
    cols = sorted(grant.update)
    pk = PK_CONFLICT[table]
    assert pk is not None
    sql, params = BUILDERS[table](docs.a.org, docs.a.owner, docs.a.ids())
    # Force the collision by naming B's key in the proposed row.
    victim = docs.b.pk(table)
    for column in pk:
        sql, params = _force_key(sql, params, table, column, victim[column])
    set_clause = f"{cols[0]} = EXCLUDED.{cols[0]}" if cols else f"{pk[0]} = EXCLUDED.{pk[0]}"
    upsert = f"{sql} ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {set_clause}"
    with pytest.raises(DBAPIError) as info, app_tx(engines[role], org=docs.a.org) as conn:
        conn.execute(text(upsert), params)
    # RLS USING failure on the existing row, a privilege error, or an FK/check failure of the
    # forged row; in every case a refusal.
    assert sqlstate(info.value) in ("42501", "23503", "23514"), info.value.orig


def _force_key(
    sql: str, params: dict[str, object], table: str, column: str, value: object
) -> tuple[str, dict[str, object]]:
    """Point the builder's INSERT at B's key by overriding the matching bind parameter."""
    name = {
        ("ingestion_checkpoints", "revision_id"): "r",
        ("ingestion_checkpoints", "stage"): None,
        ("document_publications", "document_id"): "d",
    }.get((table, column), "id")
    out = dict(params)
    if name is None:  # stage is a literal in the builder ('chunk'); B's seeded one is 'extract'
        return sql.replace("'chunk'", "'extract'"), out
    out[name] = value
    return sql, out


# -- transaction-local context: no leak through pooled connections ----------------------------


@pytest.mark.parametrize("role", ROLES)
def test_context_does_not_leak_to_the_next_transaction_on_a_reused_connection(
    app_url: URL, worker_url: URL, docs: DocWorld, role: str
) -> None:
    """One physical connection (pool of 1): context A in transaction 1 is gone in transaction 2,
    for commit, rollback and an aborted (failed) transaction, across every readable table."""
    engine = create_engine(app_url if role == "app" else worker_url, pool_size=1, max_overflow=0)
    readable = [t for t in DOC_TABLES if GRANTS[role][t].select]
    try:
        for ending in ("commit", "rollback", "error"):
            with engine.connect() as conn:
                pid = scalar(conn, "SELECT pg_backend_pid()")
                conn.rollback()
            with engine.connect() as conn:
                trans = conn.begin()
                set_ctx(conn, org=docs.a.org)
                for t in readable:
                    assert scalar(conn, f"SELECT count(*) FROM {t}") >= 1, t
                if ending == "commit":
                    trans.commit()
                elif ending == "rollback":
                    trans.rollback()
                else:
                    with pytest.raises(DBAPIError):
                        conn.execute(text("SELECT 1/0"))
                    trans.rollback()
            with engine.connect() as conn:  # same backend, "next request"
                assert scalar(conn, "SELECT pg_backend_pid()") == pid
                assert (
                    scalar(conn, "SELECT coalesce(current_setting('app.current_org_id', true), '')")
                    == ""
                )
                for t in readable:
                    assert scalar(conn, f"SELECT count(*) FROM {t}") == 0, (ending, t)
                conn.rollback()
    finally:
        engine.dispose()


@pytest.mark.parametrize("role", ROLES)
def test_back_to_back_tenants_on_one_connection_never_mix(
    app_url: URL, worker_url: URL, docs: DocWorld, role: str
) -> None:
    engine = create_engine(app_url if role == "app" else worker_url, pool_size=1, max_overflow=0)
    readable = [t for t in DOC_TABLES if GRANTS[role][t].select]
    try:
        for scope in (docs.a, docs.b, docs.a, docs.b):
            with engine.connect() as conn, conn.begin():
                set_ctx(conn, org=scope.org)
                for t in readable:
                    orgs = set(conn.execute(text(f"SELECT org_id FROM {t}")).scalars())
                    assert orgs == {scope.org}, (t, orgs)
    finally:
        engine.dispose()


@pytest.mark.parametrize("role", ROLES)
def test_session_level_set_config_is_masked_by_every_transaction_local_override(
    app_url: URL, worker_url: URL, docs: DocWorld, role: str
) -> None:
    """NOTED (same as test_05 for the identity tables): a role can poison its own session with
    ``set_config(.., false)``; a request that sets context transaction-locally (even to the empty
    string) is unaffected. Only code that sets NOTHING inherits the poison, which is why
    app.core.db and the worker session factory always set the org, empty when absent."""
    engine = create_engine(app_url if role == "app" else worker_url, pool_size=1, max_overflow=0)
    readable = [t for t in DOC_TABLES if GRANTS[role][t].select]
    try:
        with engine.connect() as conn:
            conn.execute(
                text("SELECT set_config('app.current_org_id', :o, false)"), {"o": str(docs.b.org)}
            )
            conn.commit()
        with engine.connect() as conn, conn.begin():
            set_ctx(conn, org=docs.a.org)
            for t in readable:
                assert set(conn.execute(text(f"SELECT org_id FROM {t}")).scalars()) == {docs.a.org}
        with engine.connect() as conn, conn.begin():
            set_ctx(conn, org=None)  # explicit empty
            for t in readable:
                assert scalar(conn, f"SELECT count(*) FROM {t}") == 0, t
        with engine.connect() as conn, conn.begin():
            # the documented gap: nothing set locally -> the poisoned session value is in force
            assert scalar(conn, "SELECT count(*) FROM outbox_events") >= 1
            conn.execute(text("RESET ALL"))
            assert scalar(conn, "SELECT count(*) FROM outbox_events") == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(
    "reset_sql",
    [
        "RESET app.current_org_id",
        "RESET ALL",
        "SET LOCAL app.current_org_id TO DEFAULT",
        "SET LOCAL app.current_org_id = ''",
        "SELECT set_config('app.current_org_id', NULL, true)",
        "SELECT set_config('app.current_org_id', '', false)",
    ],
)
def test_resetting_the_context_inside_a_transaction_fails_closed(
    engines: dict[str, Engine], docs: DocWorld, role: str, reset_sql: str
) -> None:
    readable = [t for t in DOC_TABLES if GRANTS[role][t].select]
    with app_tx(engines[role], org=docs.a.org) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {readable[0]}") >= 1
        conn.execute(text(reset_sql))
        for t in readable:
            assert scalar(conn, f"SELECT count(*) FROM {t}") == 0, t


@pytest.mark.parametrize("role", ROLES)
def test_discard_all_clears_context(engines: dict[str, Engine], docs: DocWorld, role: str) -> None:
    with engines[role].connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(
            text("SELECT set_config('app.current_org_id', :o, false)"), {"o": str(docs.a.org)}
        )
        assert scalar(conn, "SELECT count(*) FROM outbox_events") >= 1
        conn.execute(text("DISCARD ALL"))
        assert scalar(conn, "SELECT count(*) FROM outbox_events") == 0


@pytest.mark.parametrize(("role", "table"), SELECT_CASES, ids=ids(SELECT_CASES))
def test_row_security_off_cannot_disable_the_policy(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    """``SET row_security = off`` makes a policy-bound query ERROR for a non-bypass role, it
    never turns the policy off (and the error reveals nothing)."""
    with pytest.raises(DBAPIError) as info, app_tx(engines[role], org=docs.a.org) as conn:
        conn.execute(text("SET LOCAL row_security = off"))
        conn.execute(text(f"SELECT count(*) FROM {table}"))
    assert sqlstate(info.value) == "42501"
    assert "row-level security" in str(info.value.orig)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "SET session_replication_role = replica",  # would skip FK + immutability triggers
        "SET LOCAL session_replication_role = replica",
        "SET ROLE ukos_admin",
        "SET SESSION AUTHORIZATION ukos_admin",
        "SET ROLE postgres",
        "SET ROLE pg_read_all_data",
        "SET ROLE pg_write_all_data",
        "SET ROLE pg_write_server_files",
        "SET ROLE pg_execute_server_program",
        "RESET SESSION AUTHORIZATION; SET ROLE ukos_admin",
    ],
)
def test_role_cannot_escape_or_disable_triggers(
    engines: dict[str, Engine], docs: DocWorld, role: str, sql: str
) -> None:
    other = "ukos_worker" if role == "app" else "ukos_app"
    for statement in (sql, sql.replace("ukos_admin", other)):
        with pytest.raises(DBAPIError) as info, app_tx(engines[role], org=docs.a.org) as conn:
            for part in statement.split("; "):
                conn.execute(text(part))
        assert sqlstate(info.value) in ("42501", "22023"), (statement, info.value.orig)


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
@pytest.mark.parametrize(
    "ddl",
    [
        "ALTER TABLE {t} DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY",
        "CREATE POLICY pwn ON {t} FOR ALL USING (true)",
        "CREATE POLICY pwn ON {t} AS RESTRICTIVE FOR ALL USING (true)",
        "DROP POLICY {t}_select ON {t}",
        "ALTER TABLE {t} OWNER TO CURRENT_USER",
        "ALTER TABLE {t} DISABLE TRIGGER ALL",
        "DROP TABLE {t}",
        "ALTER TABLE {t} ADD COLUMN pwn text",
    ],
)
def test_role_cannot_alter_tables_policies_or_grants(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str, ddl: str
) -> None:
    expect_denied(engines[role], ddl.format(t=table), org=docs.a.org)


# -- soft-deleted rows: what RLS does and does not decide -------------------------------------


@pytest.mark.parametrize(("role", "table"), SELECT_CASES, ids=ids(SELECT_CASES))
def test_soft_deleted_document_stays_org_scoped_documented(
    admin_engine: Engine,
    role_names: dict[str, str],
    docs: DocWorld,
    role: str,
    table: str,
) -> None:
    """DOCUMENTED: RLS keys on org only; ``deleted_at`` is not a policy predicate (queries and
    the API hide soft-deleted documents, ADR-018). What RLS must still guarantee: a soft-deleted
    document is never visible from ANOTHER org, and the owning org's view is unchanged."""
    a = docs.a
    with admin_tx(admin_engine) as conn:
        conn.execute(
            text("UPDATE documents SET deleted_at = now(), deleted_by = :u WHERE id = :d"),
            {"u": a.owner, "d": a.doc},
        )
        become(conn, role_names[role], org=docs.b.org)
        assert scalar(conn, f"SELECT count(*) FROM {table} WHERE org_id = :a", {"a": a.org}) == 0
        if role == "app" and table == "documents":
            assert scalar(conn, "SELECT count(*) FROM documents WHERE id = :d", {"d": a.doc}) == 0
        set_ctx(conn, org=a.org)
        orgs = set(conn.execute(text(f"SELECT org_id FROM {table}")).scalars())
        assert orgs == {a.org}


@pytest.mark.parametrize("role", ROLES)
def test_soft_deleted_org_never_exposes_rows_to_another_org_documented(
    admin_engine: Engine, role_names: dict[str, str], docs: DocWorld, role: str
) -> None:
    """DOCUMENTED: a soft-deleted org's rows remain addressable by its own context (the API
    answers 404 before any query, M01 §contract); they are still invisible to every other org."""
    a, b = docs.a, docs.b
    readable = [t for t in DOC_TABLES if GRANTS[role][t].select]
    with admin_tx(admin_engine) as conn:
        conn.execute(
            text("UPDATE organizations SET deleted_at = now() WHERE id = :o"), {"o": a.org}
        )
        become(conn, role_names[role], org=b.org)
        for t in readable:
            orgs = set(conn.execute(text(f"SELECT org_id FROM {t}")).scalars())
            assert orgs == {b.org}, t
        set_ctx(conn, org=a.org)
        for t in readable:
            assert scalar(conn, f"SELECT count(*) FROM {t} WHERE org_id = :b", {"b": b.org}) == 0


# -- the worker role on the pre-existing context tables ----------------------------------------


def test_worker_sees_only_the_context_org_in_workspaces_and_organizations(
    worker_engine: Engine, world: World
) -> None:
    a, b = world.a, world.b
    with app_tx(worker_engine) as conn:
        assert scalar(conn, "SELECT count(*) FROM workspaces") == 0
        assert scalar(conn, "SELECT count(*) FROM organizations") == 0
        assert scalar(conn, "SELECT count(*) FROM organization_memberships") == 0
    with app_tx(worker_engine, org=a.org_id) as conn:
        assert set(conn.execute(text("SELECT org_id FROM workspaces")).scalars()) == {a.org_id}
        assert set(conn.execute(text("SELECT id FROM organizations")).scalars()) == {a.org_id}
        assert set(conn.execute(text("SELECT org_id FROM organization_memberships")).scalars()) == {
            a.org_id
        }
        assert (
            scalar(conn, "SELECT count(*) FROM workspaces WHERE org_id = :b", {"b": b.org_id}) == 0
        )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT role FROM organization_memberships",
        "SELECT * FROM organization_memberships",
        "SELECT count(*) FROM users",
        "SELECT count(*) FROM resource_grants",
        "SELECT count(*) FROM workspace_memberships",
        "SELECT count(*) FROM audit_events",
        "SELECT count(*) FROM upload_sessions",
        "UPDATE documents SET title = 'pwn'",
        "UPDATE document_versions SET purged_at = now()",
        "UPDATE workspaces SET name = 'pwn'",
        "UPDATE organizations SET name = 'pwn'",
        "DELETE FROM documents",
        "DELETE FROM workspaces",
        "INSERT INTO workspaces (id, org_id, slug, name, created_by)"
        " VALUES (gen_random_uuid(), gen_random_uuid(), 'x', 'x', gen_random_uuid())",
    ],
)
def test_worker_has_no_access_beyond_its_grants(
    worker_engine: Engine, world: World, sql: str
) -> None:
    expect_denied(worker_engine, sql, match="permission denied", org=world.a.org_id)


AUDIT_INSERT = (
    "INSERT INTO audit_events (id, org_id, actor_user_id, event_type, target_type, target_id,"
    " outcome) VALUES (:id, :o, NULL, 'document.version.indexed', 'document', 'x', 'success')"
)


def test_worker_audit_insert_only_for_the_context_org(worker_engine: Engine, world: World) -> None:
    with app_tx(worker_engine, org=world.a.org_id) as conn:
        assert (
            conn.execute(text(AUDIT_INSERT), {"id": uuid.uuid4(), "o": world.a.org_id}).rowcount
            == 1
        )
    expect_denied(
        worker_engine,
        AUDIT_INSERT,
        {"id": uuid.uuid4(), "o": world.b.org_id},
        match="row-level security",
        org=world.a.org_id,
    )
    expect_denied(
        worker_engine,
        AUDIT_INSERT,
        {"id": uuid.uuid4(), "o": world.a.org_id},
        match="row-level security",
    )


def test_worker_cannot_read_back_or_change_audit_events(
    worker_engine: Engine, world: World
) -> None:
    for sql in (
        "SELECT count(*) FROM audit_events",
        "UPDATE audit_events SET outcome = 'error'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ):
        expect_denied(worker_engine, sql, match="permission denied", org=world.a.org_id)
    # even RETURNING (which needs SELECT) is refused
    expect_denied(
        worker_engine,
        AUDIT_INSERT + " RETURNING id",
        {"id": uuid.uuid4(), "o": world.a.org_id},
        match="permission denied",
        org=world.a.org_id,
    )


# ruff: noqa: PT017
