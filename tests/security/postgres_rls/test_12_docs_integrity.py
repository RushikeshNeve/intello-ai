"""Column-level privileges, defence in depth under hypothetical extra grants, and the immutability
triggers of the M02/M03 tables, for both roles and for the table owner.

* column matrix: for every column of every table, ``UPDATE t SET c = c`` as each role is allowed
  exactly when the registry grants it;
* probe grants (rolled back): even if DELETE / UPDATE(org_id) / TRUNCATE were granted, the RLS
  default-deny and WITH CHECK still hold;
* ``document_versions`` is immutable and ``ingestion_job_events`` is append-only for every role
  including a non-superuser owner; ``upload_sessions.state`` only moves forward.
"""

from __future__ import annotations

import uuid

import pytest
from rls_docs_world import (
    CASE_IDS,
    CASES,
    DOC_TABLES,
    GRANTS,
    ROLES,
    DocWorld,
    admin_tx,
    become,
    pk_params,
    pk_where,
)
from rls_world import admin_count, app_tx, expect_denied, scalar, set_ctx, sqlstate
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

# -- column-level UPDATE matrix --------------------------------------------------------------


def _columns(admin: Engine, table: str) -> list[str]:
    with admin.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = :t"
                    " AND is_identity = 'NO' ORDER BY ordinal_position"
                ),
                {"t": table},
            ).scalars()
        )


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_update_privilege_matrix_over_every_column(
    engines: dict[str, Engine], admin_engine: Engine, docs: DocWorld, role: str, table: str
) -> None:
    """A role may UPDATE exactly the registry's columns of its own org's rows; any other column
    (ids, org_id, workspace_id, the fencing epoch, attempts, ...) is a privilege error."""
    granted = GRANTS[role][table].update
    own = admin_count(
        admin_engine, f"SELECT count(*) FROM {table} WHERE org_id = :o", {"o": docs.a.org}
    )
    for column in _columns(admin_engine, table):
        sql = f"UPDATE {table} SET {column} = {column}"
        if column in granted:
            with app_tx(engines[role], org=docs.a.org) as conn:
                assert conn.execute(text(sql)).rowcount == own, column
        else:
            expect_denied(engines[role], sql, match="permission denied", org=docs.a.org)


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_select_for_update_is_org_scoped_and_needs_update_privilege(
    engines: dict[str, Engine], docs: DocWorld, role: str, table: str
) -> None:
    """Row locks (FOR UPDATE) respect RLS and are denied without any UPDATE privilege, so a role
    cannot lock (and so stall) another org's rows."""
    grant = GRANTS[role][table]
    sql = f"SELECT count(*) FROM (SELECT 1 FROM {table} WHERE {pk_where(table)} FOR UPDATE) x"
    if not grant.update:
        expect_denied(
            engines[role],
            sql,
            pk_params(docs.a.pk(table)),
            match="permission denied",
            org=docs.a.org,
        )
        return
    with app_tx(engines[role], org=docs.a.org) as conn:
        assert scalar(conn, sql, pk_params(docs.b.pk(table))) == 0
        assert scalar(conn, sql, pk_params(docs.a.pk(table))) == 1


# -- probe grants: RLS holds even if the privilege layer were widened ---------------------------


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_extra_delete_grant_still_deletes_nothing(
    admin_engine: Engine, role_names: dict[str, str], docs: DocWorld, role: str, table: str
) -> None:
    """No table has a DELETE policy. Grant DELETE to the role (rolled back): RLS default-deny
    still leaves every row in place, including the role's own org's."""
    with admin_tx(admin_engine) as conn:
        before = scalar(conn, f"SELECT count(*) FROM {table}")
        conn.execute(text(f'GRANT DELETE ON "{table}" TO "{role_names[role]}"'))
        become(conn, role_names[role], org=docs.a.org)
        assert conn.execute(text(f"DELETE FROM {table}")).rowcount == 0
        conn.execute(text("RESET ROLE"))
        assert scalar(conn, f"SELECT count(*) FROM {table}") == before


@pytest.mark.parametrize("role", ROLES)
def test_extra_truncate_grant_cannot_truncate_the_append_only_log(
    admin_engine: Engine, role_names: dict[str, str], docs: DocWorld, role: str
) -> None:
    """TRUNCATE bypasses RLS entirely, so the privilege must never be granted (test_10). The one
    table that also defends itself is the append-only event log: a BEFORE TRUNCATE trigger."""
    table = "ingestion_job_events"
    with admin_tx(admin_engine) as conn:
        conn.execute(text(f'GRANT TRUNCATE ON "{table}" TO "{role_names[role]}"'))
        become(conn, role_names[role], org=docs.a.org)
        with pytest.raises(DBAPIError) as info:
            conn.execute(text(f"TRUNCATE {table}"))
    assert sqlstate(info.value) == "42501"
    assert "append-only" in str(info.value.orig)


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_update_policy_with_check_blocks_moving_rows_across_orgs(
    admin_engine: Engine, role_names: dict[str, str], docs: DocWorld, role: str, table: str
) -> None:
    """Probe: grant ``UPDATE (org_id)`` (rolled back) and try to move A's own row into org B.
    The UPDATE policy's WITH CHECK (or, where there is no UPDATE policy, default deny) stops it."""
    a, b = docs.a, docs.b
    has_update_policy = table not in ("ingestion_job_events", "outbox_events")
    with admin_tx(admin_engine) as conn:
        conn.execute(text(f'GRANT UPDATE (org_id) ON "{table}" TO "{role_names[role]}"'))
        become(conn, role_names[role], org=a.org)
        sql = f"UPDATE {table} SET org_id = :b WHERE {pk_where(table)}"
        params = {"b": b.org, **pk_params(a.pk(table))}
        if not GRANTS[role][table].select:
            # UPDATE ... WHERE needs SELECT on the columns the WHERE reads: privilege error.
            with pytest.raises(DBAPIError) as info:
                conn.execute(text(sql), params)
            assert sqlstate(info.value) == "42501"
            return
        if has_update_policy:
            with pytest.raises(DBAPIError) as info:
                conn.execute(text(sql), params)
            assert sqlstate(info.value) == "42501", info.value.orig
            message = str(info.value.orig)
            assert "row-level security" in message or "immutable" in message, message
            return  # the transaction is aborted; the row is untouched by construction
        # no UPDATE policy -> the row is invisible to UPDATE: 0 rows, nothing moved
        assert conn.execute(text(sql), params).rowcount == 0
        conn.execute(text("RESET ROLE"))
        assert (
            scalar(conn, f"SELECT count(*) FROM {table} WHERE {pk_where(table)} AND org_id = :a",
                   {**pk_params(a.pk(table)), "a": a.org})
            == 1
        )  # fmt: skip


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_extra_update_grant_still_cannot_reach_other_orgs_rows(
    admin_engine: Engine, role_names: dict[str, str], docs: DocWorld, role: str, table: str
) -> None:
    """Probe: grant UPDATE on a harmless column; org A's context still updates 0 of B's rows."""
    column = {
        "sources": "name",
        "documents": "title",
        "document_versions": "purged_at",
        "upload_sessions": "state",
        "ingestion_jobs": "state",
        "ingestion_job_events": "stage",
        "ingestion_checkpoints": "state",
        "knowledge_revisions": "state",
        "document_publications": "reconciled_at",
        "outbox_events": "last_error",
    }[table]
    with admin_tx(admin_engine) as conn:
        conn.execute(text(f'GRANT UPDATE ({column}) ON "{table}" TO "{role_names[role]}"'))
        become(conn, role_names[role], org=docs.a.org)
        sql = f"UPDATE {table} SET {column} = {column} WHERE {pk_where(table)}"
        if not GRANTS[role][table].select:
            # the WHERE clause itself needs SELECT: refused before any row is considered
            with pytest.raises(DBAPIError) as info:
                conn.execute(text(sql), pk_params(docs.b.pk(table)))
            assert sqlstate(info.value) == "42501"
            return
        assert conn.execute(text(sql), pk_params(docs.b.pk(table))).rowcount == 0


# -- immutability / append-only, for every role including a non-superuser owner ---------------

# Expression producing a DIFFERENT value for each immutable document_versions column.
_VERSION_CHANGES = {
    "id": "gen_random_uuid()",
    "org_id": "gen_random_uuid()",
    "workspace_id": "gen_random_uuid()",
    "document_id": "gen_random_uuid()",
    "version_number": "version_number + 1",
    "original_filename": "original_filename || 'x'",
    "content_type": "content_type || 'x'",
    "size_bytes": "size_bytes + 1",
    "sha256": "repeat('b', 64)",
    "object_key": "object_key || 'x'",
    "created_by": "gen_random_uuid()",
    "created_at": "created_at + interval '1 day'",
}


def _owner_probe(conn: Connection, table: str, org: uuid.UUID, *, bypass: bool = False) -> None:
    """Hand ``table`` to a throwaway non-superuser owner and act as it with org context.

    ``bypass=True`` also drops FORCE ROW LEVEL SECURITY, i.e. the owner is exempt from policies
    (the PostgreSQL default for an owner): only the triggers are left to stop it."""
    conn.execute(text("CREATE ROLE rls_probe_owner_docs NOLOGIN NOSUPERUSER NOBYPASSRLS"))
    conn.execute(text(f"ALTER TABLE {table} OWNER TO rls_probe_owner_docs"))
    if bypass:
        conn.execute(text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
    conn.execute(text("SET LOCAL ROLE rls_probe_owner_docs"))
    set_ctx(conn, org=org)


@pytest.mark.parametrize("column", sorted(_VERSION_CHANGES))
@pytest.mark.parametrize("actor", ["admin", "owner", "owner_bypass", "app"])
def test_document_version_columns_are_immutable(
    admin_engine: Engine,
    role_names: dict[str, str],
    docs: DocWorld,
    actor: str,
    column: str,
) -> None:
    """Superuser/owner (admin), a non-superuser owner under FORCE RLS, and the app role (for the
    one column it may even name, ``purged_at``, see below) all get 42501 from the trigger."""
    a = docs.a
    sql = f"UPDATE document_versions SET {column} = {_VERSION_CHANGES[column]} WHERE id = :v"
    with admin_tx(admin_engine) as conn:
        if actor in ("owner", "owner_bypass"):
            _owner_probe(conn, "document_versions", a.org, bypass=actor == "owner_bypass")
        elif actor == "app":
            conn.execute(
                text(f'GRANT UPDATE ("{column}") ON document_versions TO "{role_names["app"]}"')
            )
            become(conn, role_names["app"], org=a.org)
        with pytest.raises(DBAPIError) as info:
            conn.execute(text(sql), {"v": a.version})
    assert sqlstate(info.value) == "42501", info.value.orig
    assert "immutable" in str(info.value.orig), info.value.orig


@pytest.mark.parametrize("actor", ["admin", "app"])
def test_document_version_purged_at_is_set_once(
    admin_engine: Engine, role_names: dict[str, str], docs: DocWorld, actor: str
) -> None:
    a = docs.a
    with admin_tx(admin_engine) as conn:
        if actor == "app":
            become(conn, role_names["app"], org=a.org)
        upd = "UPDATE document_versions SET purged_at = {} WHERE id = :v"
        assert (
            conn.execute(text(upd.format("now()")), {"v": a.version2}).rowcount == 1
        )  # NULL -> ts
        for value in ("now() + interval '1 day'", "NULL"):  # ts -> other / ts -> NULL
            nested = conn.begin_nested()
            with pytest.raises(DBAPIError) as info:
                conn.execute(text(upd.format(value)), {"v": a.version2})
            assert sqlstate(info.value) == "42501", info.value.orig
            assert "purged_at" in str(info.value.orig)
            nested.rollback()


def test_document_versions_immutable_for_a_non_superuser_owner_purged_at_once(
    admin_engine: Engine, docs: DocWorld
) -> None:
    with admin_tx(admin_engine) as conn:
        _owner_probe(conn, "document_versions", docs.a.org)
        sql = "UPDATE document_versions SET purged_at = {} WHERE id = :v"
        assert conn.execute(text(sql.format("now()")), {"v": docs.a.version2}).rowcount == 1
        nested = conn.begin_nested()
        with pytest.raises(DBAPIError) as info:
            conn.execute(text(sql.format("now() + interval '1 day'")), {"v": docs.a.version2})
        assert sqlstate(info.value) == "42501"
        nested.rollback()


@pytest.mark.parametrize("actor", ["admin", "owner", "owner_bypass"])
@pytest.mark.parametrize("statement", ["UPDATE", "DELETE", "TRUNCATE", "TRUNCATE CASCADE"])
def test_job_events_are_append_only_even_for_admin_and_owner(
    admin_engine: Engine, docs: DocWorld, actor: str, statement: str
) -> None:
    """admin and an owner exempt from RLS (``owner_bypass``) are stopped by the trigger. An owner
    that is still bound by FORCE RLS never even sees a row to UPDATE/DELETE (no such policy)."""
    a = docs.a
    sql = {
        "UPDATE": "UPDATE ingestion_job_events SET stage = 'extract' WHERE seq = :s",
        "DELETE": "DELETE FROM ingestion_job_events WHERE seq = :s",
        "TRUNCATE": "TRUNCATE ingestion_job_events",
        "TRUNCATE CASCADE": "TRUNCATE ingestion_job_events CASCADE",
    }[statement]
    params = {"s": a.event_seq} if ":s" in sql else {}
    with admin_tx(admin_engine) as conn:
        if actor in ("owner", "owner_bypass"):
            _owner_probe(conn, "ingestion_job_events", a.org, bypass=actor == "owner_bypass")
        if actor == "owner" and statement in ("UPDATE", "DELETE"):
            assert conn.execute(text(sql), params).rowcount == 0  # default-deny under FORCE
            return
        with pytest.raises(DBAPIError) as info:
            conn.execute(text(sql), params)
    assert sqlstate(info.value) == "42501", info.value.orig
    assert "append-only" in str(info.value.orig)


@pytest.mark.parametrize("role", ROLES)
def test_job_events_append_is_the_only_write_and_it_works(
    engines: dict[str, Engine], admin_engine: Engine, docs: DocWorld, role: str
) -> None:
    a = docs.a
    insert = (
        "INSERT INTO ingestion_job_events (job_id, org_id, workspace_id, document_id, version_id,"
        " event_type) VALUES (:j, :o, :w, :d, :v, 'queued')"
    )
    params = {"j": a.job, "o": a.org, "w": a.ws, "d": a.doc2, "v": a.version2}
    with app_tx(engines[role], org=a.org) as conn:
        assert conn.execute(text(insert), params).rowcount == 1
    for sql in (
        "UPDATE ingestion_job_events SET stage = 'extract'",
        "DELETE FROM ingestion_job_events",
        "TRUNCATE ingestion_job_events",
    ):
        expect_denied(engines[role], sql, match="permission denied", org=a.org)
    assert (
        admin_count(
            admin_engine,
            "SELECT count(*) FROM ingestion_job_events WHERE seq = :s",
            {"s": a.event_seq},
        )
        == 1
    )


def test_seq_cannot_be_forged_on_insert(engines: dict[str, Engine], docs: DocWorld) -> None:
    """``seq`` is GENERATED ALWAYS: an explicit value (to overwrite or order events) is refused."""
    a = docs.a
    for role in ROLES:
        expect_denied(
            engines[role],
            "INSERT INTO ingestion_job_events (seq, job_id, org_id, workspace_id, document_id,"
            " version_id, event_type) VALUES (1, :j, :o, :w, :d, :v, 'queued')",
            {"j": a.job, "o": a.org, "w": a.ws, "d": a.doc2, "v": a.version2},
            states=("428C9",),
            org=a.org,
        )
        expect_denied(
            engines[role],
            "INSERT INTO outbox_events (seq, org_id, aggregate_type, aggregate_id, event_type)"
            " VALUES (1, :o, 'x', gen_random_uuid(), 'knowledge.reconcile')",
            {"o": a.org},
            states=("428C9",),
            org=a.org,
        )


def test_upload_session_state_only_moves_forward(
    engines: dict[str, Engine], docs: DocWorld, admin_engine: Engine, role_names: dict[str, str]
) -> None:
    a = docs.a
    done = (
        "UPDATE upload_sessions SET state = 'finalized', finalized_at = now(), failure_code = NULL"
        " WHERE id = :i"
    )
    for actor in ("app", "admin"):
        with admin_tx(admin_engine) as conn:
            if actor == "app":
                become(conn, role_names["app"], org=a.org)
            assert conn.execute(text(done), {"i": a.session}).rowcount == 1  # pending -> finalized
            for back in (
                "UPDATE upload_sessions SET state = 'pending', finalized_at = NULL WHERE id = :i",
                "UPDATE upload_sessions SET state = 'failed', failure_code = 'x',"
                " finalized_at = NULL WHERE id = :i",
            ):
                nested = conn.begin_nested()
                with pytest.raises(DBAPIError) as info:
                    conn.execute(text(back), {"i": a.session})
                assert sqlstate(info.value) == "23514", (actor, info.value.orig)
                nested.rollback()


def test_worker_cannot_touch_upload_sessions_or_sources_at_all(
    worker_engine: Engine, docs: DocWorld
) -> None:
    for sql in (
        "SELECT 1 FROM upload_sessions",
        "UPDATE upload_sessions SET state = 'failed'",
        "INSERT INTO sources (id, org_id, workspace_id, kind, name, created_by)"
        " VALUES (gen_random_uuid(), :o, :w, 'url', 'x', :u)",
    ):
        expect_denied(
            worker_engine,
            sql,
            {"o": docs.a.org, "w": docs.a.ws, "u": docs.a.owner} if ":o" in sql else None,
            match="permission denied",
            org=docs.a.org,
        )


def test_every_doc_table_is_covered_by_the_column_matrix() -> None:
    assert {t for _, t in CASES} == set(DOC_TABLES)
