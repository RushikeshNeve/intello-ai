"""Grants introspection: least privilege for ``ukos_app`` and ``ukos_worker`` (M03.01.01, §4)."""

from __future__ import annotations

import uuid

import pytest
from compose_support import ComposeProject
from m03_support import claim, insert_job, role_tx, scalar, seed_scope, sqlstate
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.core.models import RLS_TABLES
from app.modules.ingestion.models import INGESTION_TABLES

M04_CHAT_TABLES = {
    "ai_policies",
    "chat_threads",
    "chat_messages",
    "chat_runs",
    "chat_run_events",
    # M05 (migration 0008): exact grants in tests/integration/m05_graph.
    "graph_entities",
    "entity_merge_candidates",
}
# M06 (migration 0009): the sync task runs as the worker; exact grants: rls test_18.
M06_TABLES = {"sources", "connector_accounts", "source_syncs", "source_items"}
ALL_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
SEARCH_PATH = ["search_path=pg_catalog, public, pg_temp"]
DEFINER_FUNCTIONS = {
    "ingest_claim_job": "uuid, text, integer",
    "ingest_reap_expired_leases": "integer",
    "outbox_fetch_batch": "integer",
    "outbox_mark_dispatched": "uuid[]",
    "outbox_mark_failed": "uuid, text, integer",
    "knowledge_next_reconcile_batch": "integer, integer",
}

APP_PRIVILEGES = {
    "ingestion_jobs": {"SELECT", "INSERT"},
    "ingestion_job_events": {"SELECT", "INSERT"},
    "outbox_events": {"SELECT", "INSERT"},
    "ingestion_checkpoints": {"SELECT"},
    "knowledge_revisions": {"SELECT"},
    "document_publications": {"SELECT"},
}
WORKER_PRIVILEGES = {
    "ingestion_jobs": {"SELECT", "INSERT"},
    "ingestion_job_events": {"SELECT", "INSERT"},
    "outbox_events": {"SELECT", "INSERT"},
    "ingestion_checkpoints": {"SELECT", "INSERT"},
    "knowledge_revisions": {"SELECT", "INSERT"},
    "document_publications": {"SELECT", "INSERT"},
    "documents": {"SELECT", "INSERT"},
    "document_versions": {"SELECT", "INSERT"},
    "workspaces": {"SELECT"},
    "organizations": {"SELECT"},
    "audit_events": {"INSERT"},
}
WORKER_UPDATE_COLUMNS = {
    "documents": {"latest_version_id", "updated_at", "deleted_at", "deleted_by"},
    "ingestion_jobs": {
        "state", "run_after", "lease_owner", "lease_expires_at", "heartbeat_at", "current_stage",
        "last_error_code", "last_error_detail", "updated_at", "started_at", "finished_at",
    },
    "ingestion_checkpoints": {
        "state", "input_fingerprint", "artifact_key", "artifact_sha256", "artifact_bytes",
        "attempts", "started_at", "completed_at", "error_code",
    },
    "knowledge_revisions": {
        "state", "chunk_count", "content_fingerprint", "ready_at", "published_at",
        "superseded_at", "purged_at",
    },
    "document_publications": {
        "revision_id", "version_id", "version_number", "previous_revision_id", "published_at",
        "reconciled_at",
    },
}  # fmt: skip


def _table_privileges(engine: Engine, role: str, table: str) -> set[str]:
    with engine.connect() as conn:
        return {
            p
            for p in ALL_PRIVILEGES
            if conn.execute(
                text("SELECT has_table_privilege(:r, CAST(:t AS regclass), :p)"),
                {"r": role, "t": f"public.{table}", "p": p},
            ).scalar_one()
        }


def _update_columns(engine: Engine, role: str, table: str) -> set[str]:
    with engine.connect() as conn:
        columns = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        ).scalars()
        return {
            c
            for c in columns
            if conn.execute(
                text("SELECT has_column_privilege(:r, CAST(:t AS regclass), :c, 'UPDATE')"),
                {"r": role, "t": f"public.{table}", "c": c},
            ).scalar_one()
        }


@pytest.mark.parametrize("table", INGESTION_TABLES)
def test_app_role_privileges_on_ingestion_tables(
    admin_engine: Engine, stack: ComposeProject, migrated: None, table: str
) -> None:
    app = stack.env["POSTGRES_APP_USER"]
    assert _table_privileges(admin_engine, app, table) == APP_PRIVILEGES[table]
    assert _update_columns(admin_engine, app, table) == set()  # no UPDATE on any column


@pytest.mark.parametrize("table", sorted(WORKER_PRIVILEGES))
def test_worker_privileges_are_exact(
    admin_engine: Engine, stack: ComposeProject, migrated: None, table: str
) -> None:
    worker = stack.env["POSTGRES_WORKER_USER"]
    assert _table_privileges(admin_engine, worker, table) == WORKER_PRIVILEGES[table]
    assert _update_columns(admin_engine, worker, table) == WORKER_UPDATE_COLUMNS.get(table, set())


def test_worker_membership_access_is_two_columns_only(
    admin_engine: Engine, stack: ComposeProject, migrated: None
) -> None:
    """``organizations_select`` evaluates an EXISTS over organization_memberships with the caller's
    privileges, so the worker needs exactly these columns, read-only."""
    worker = stack.env["POSTGRES_WORKER_USER"]
    assert _table_privileges(admin_engine, worker, "organization_memberships") == set()
    with admin_engine.connect() as conn:
        selectable = {
            c
            for c in ("org_id", "user_id", "role", "created_at")
            if conn.execute(
                text(
                    "SELECT has_column_privilege(:r, 'public.organization_memberships', :c,"
                    " 'SELECT')"
                ),
                {"r": worker, "c": c},
            ).scalar_one()
        }
    assert selectable == {"org_id", "user_id"}
    assert _update_columns(admin_engine, worker, "organization_memberships") == set()


def test_worker_job_columns_it_cannot_change(admin_engine: Engine, stack: ComposeProject) -> None:
    """Identity, ownership, the attempt counter and the fencing epoch move only through the
    definer functions (or never)."""
    held = _update_columns(admin_engine, stack.env["POSTGRES_WORKER_USER"], "ingestion_jobs")
    for column in (
        "id", "org_id", "workspace_id", "document_id", "version_id", "revision_id",
        "pipeline_version", "embedding_space_id", "attempts", "max_attempts", "lease_epoch",
        "created_at",
    ):  # fmt: skip
        assert column not in held, column


def test_no_role_has_delete_truncate_references_or_trigger_anywhere(
    admin_engine: Engine, stack: ComposeProject, migrated: None
) -> None:
    app, worker = stack.env["POSTGRES_APP_USER"], stack.env["POSTGRES_WORKER_USER"]
    with admin_engine.connect() as conn:
        tables = list(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
    assert set(RLS_TABLES) <= set(tables)
    forbidden = {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
    # The worker holds none of them on ANY table; the API role none on the M03 tables (the two
    # M01 membership tables keep the DELETE grant that removing members needs).
    for table in tables:
        held = _table_privileges(admin_engine, worker, table)
        assert held.isdisjoint(forbidden), (worker, table)
    for table in INGESTION_TABLES:
        held = _table_privileges(admin_engine, app, table)
        assert held.isdisjoint(forbidden), (app, table)


def test_worker_reaches_nothing_beyond_its_grants(
    admin_engine: Engine, stack: ComposeProject, migrated: None
) -> None:
    worker = stack.env["POSTGRES_WORKER_USER"]
    with admin_engine.connect() as conn:
        reachable = set(
            conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = 'public' AND c.relkind IN ('r','v','m','p','f')"
                    " AND (has_table_privilege(:r, c.oid, 'SELECT')"
                    "   OR has_table_privilege(:r, c.oid, 'INSERT')"
                    "   OR has_table_privilege(:r, c.oid, 'UPDATE')"
                    "   OR has_table_privilege(:r, c.oid, 'DELETE'))"
                ),
                {"r": worker},
            ).scalars()
        )
    # alembic_version, users, memberships, grants, sources, upload_sessions: out of reach.
    assert reachable - M04_CHAT_TABLES - M06_TABLES == set(WORKER_PRIVILEGES)


def test_worker_cannot_create_objects_or_alter_the_schema(
    worker_engine: Engine, migrated: None
) -> None:
    with worker_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT has_schema_privilege('public', 'CREATE'),"
                " has_database_privilege(current_database(), 'CREATE'),"
                " (SELECT rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb"
                "  FROM pg_roles WHERE rolname = current_user)"
            )
        ).one()
    assert tuple(row) == (False, False, False)
    for ddl in (
        "ALTER TABLE ingestion_jobs DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE ingestion_jobs NO FORCE ROW LEVEL SECURITY",
        "CREATE POLICY pwn ON ingestion_jobs FOR SELECT USING (true)",
        "TRUNCATE ingestion_jobs",
        "DROP TABLE outbox_events",
        "CREATE TABLE pwn (x int)",
        "ALTER TABLE ingestion_job_events DISABLE TRIGGER ALL",
    ):
        with pytest.raises(DBAPIError) as exc, worker_engine.begin() as conn:
            conn.execute(text(ddl))
        assert sqlstate(exc.value) == "42501", ddl


# -- the SECURITY DEFINER functions ------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(DEFINER_FUNCTIONS))
def test_definer_function_hardening(
    admin_engine: Engine, stack: ComposeProject, migrated: None, name: str
) -> None:
    signature = f"public.{name}({DEFINER_FUNCTIONS[name]})"
    app, worker = stack.env["POSTGRES_APP_USER"], stack.env["POSTGRES_WORKER_USER"]
    with admin_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner) AS owner,"
                " p.proacl::text AS acl, p.proowner = c.oid AS owned_by_migration_user"
                " FROM pg_proc p, pg_roles c"
                " WHERE p.oid = CAST(:s AS regprocedure) AND c.rolname = current_user"
            ),
            {"s": signature},
        ).one()
        can = {
            role: conn.execute(
                text("SELECT has_function_privilege(:r, CAST(:s AS regprocedure), 'EXECUTE')"),
                {"r": role, "s": signature},
            ).scalar_one()
            for role in (app, worker)
        }
    assert row.prosecdef is True
    assert row.proconfig == SEARCH_PATH  # pg_temp last: temp objects can never shadow ours
    assert row.owner == stack.env["POSTGRES_USER"]  # the migration user
    assert row.owned_by_migration_user
    assert can == {app: False, worker: True}
    # REVOKE ALL ... FROM PUBLIC: no "=X/..." entry; only the owner and the worker are listed.
    grantees = {entry.split("=")[0] for entry in row.acl.strip("{}").split(",")}
    assert grantees == {stack.env["POSTGRES_USER"], worker}, row.acl


def test_append_only_trigger_function_is_not_callable(
    admin_engine: Engine, stack: ComposeProject, migrated: None
) -> None:
    with admin_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT prosecdef, proconfig,"
                " has_function_privilege(:a, oid, 'EXECUTE'),"
                " has_function_privilege(:w, oid, 'EXECUTE')"
                " FROM pg_proc WHERE proname = 'ukos_ingestion_job_events_append_only'"
            ),
            {"a": stack.env["POSTGRES_APP_USER"], "w": stack.env["POSTGRES_WORKER_USER"]},
        ).one()
    assert tuple(row) == (False, SEARCH_PATH, False, False)


@pytest.mark.parametrize(
    "call",
    [
        "SELECT * FROM ingest_claim_job(gen_random_uuid(), 'w', 60)",
        "SELECT ingest_reap_expired_leases(10)",
        "SELECT * FROM outbox_fetch_batch(10)",
        "SELECT outbox_mark_dispatched(ARRAY[gen_random_uuid()])",
        "SELECT outbox_mark_failed(gen_random_uuid(), 'x', 1)",
        "SELECT * FROM knowledge_next_reconcile_batch(10, 10)",
    ],
)
def test_the_api_role_cannot_call_the_definer_functions(
    app_engine: Engine, migrated: None, call: str
) -> None:
    with pytest.raises(DBAPIError) as exc, role_tx(app_engine, uuid.uuid4()) as conn:
        conn.execute(text(call))
    assert sqlstate(exc.value) == "42501"
    assert "permission denied for function" in str(exc.value.orig)


def test_search_path_hijack_cannot_redirect_a_definer_function(
    worker_engine: Engine, admin_engine: Engine, migrated: None
) -> None:
    """A temp table shadowing ``ingestion_jobs`` in the CALLER's search_path changes nothing: the
    function's pinned path (pg_catalog, public, pg_temp) resolves the real, schema-qualified one."""
    scope = seed_scope(admin_engine)
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope)
    with role_tx(worker_engine, None) as conn:
        conn.execute(text("CREATE TEMP TABLE ingestion_jobs (LIKE public.ingestion_jobs)"))
        conn.execute(
            text("CREATE TEMP TABLE ingestion_job_events (LIKE public.ingestion_job_events)")
        )
        conn.execute(text("SET LOCAL search_path = pg_temp, public"))
        assert len(claim(conn, job)) == 1
        assert scalar(conn, "SELECT count(*) FROM pg_temp.ingestion_jobs") == 0
        assert scalar(conn, "SELECT count(*) FROM pg_temp.ingestion_job_events") == 0
    with admin_engine.connect() as conn:
        assert scalar(conn, "SELECT state FROM ingestion_jobs WHERE id = :j", j=job) == "running"
        assert (
            scalar(
                conn,
                "SELECT count(*) FROM ingestion_job_events WHERE job_id = :j"
                " AND event_type = 'started'",
                j=job,
            )
            == 1
        )
