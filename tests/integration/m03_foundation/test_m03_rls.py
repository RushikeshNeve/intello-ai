"""Row-level security of the ingestion tables for BOTH least-privilege roles (M03.01.01, §4, §12).

The policies carry no ``TO`` clause, so ``ukos_app`` and ``ukos_worker`` are bound identically:
no context -> nothing; org A's context -> only org A; writes naming another org are rejected.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from m03_support import (
    Scope,
    insert_job,
    insert_outbox,
    insert_publication,
    insert_revision,
    role_tx,
    scalar,
    seed_scope,
    sqlstate,
)
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from app.modules.ingestion.models import INGESTION_TABLES

EVENT_INSERT = (
    "INSERT INTO ingestion_job_events (job_id, org_id, workspace_id, document_id, version_id,"
    " event_type) VALUES (:j, :o, :w, :d, :v, 'queued')"
)


@dataclass(frozen=True)
class Seeded:
    scope: Scope
    job: uuid.UUID
    revision: uuid.UUID


def _seed(engine: Engine) -> Seeded:
    scope = seed_scope(engine)
    with engine.begin() as conn:
        job = insert_job(conn, scope)
        conn.execute(
            text(EVENT_INSERT),
            {
                "j": job,
                "o": scope.org_id,
                "w": scope.workspace_id,
                "d": scope.document_id,
                "v": scope.version_id,
            },
        )
        revision = insert_revision(conn, scope)
        conn.execute(
            text(
                "INSERT INTO ingestion_checkpoints (revision_id, stage, org_id, input_fingerprint)"
                " VALUES (:r, 'extract', :o, 'fp')"
            ),
            {"r": revision, "o": scope.org_id},
        )
        insert_publication(conn, scope, revision)
        insert_outbox(conn, scope)
    return Seeded(scope, job, revision)


@pytest.fixture
def a(admin_engine: Engine, migrated: None) -> Seeded:
    return _seed(admin_engine)


@pytest.fixture
def b(admin_engine: Engine, migrated: None) -> Seeded:
    return _seed(admin_engine)


@pytest.fixture(params=["app", "worker"])
def engine(request: pytest.FixtureRequest, app_engine: Engine, worker_engine: Engine) -> Engine:
    return app_engine if request.param == "app" else worker_engine


def _count(conn: Connection, table: str, org_id: uuid.UUID | None = None) -> int:
    if org_id is None:
        return int(scalar(conn, f"SELECT count(*) FROM {table}"))
    return int(scalar(conn, f"SELECT count(*) FROM {table} WHERE org_id = :o", o=org_id))


@pytest.mark.parametrize("table", INGESTION_TABLES)
def test_no_context_sees_nothing(engine: Engine, a: Seeded, table: str) -> None:
    with role_tx(engine, None) as conn:
        assert _count(conn, table) == 0


@pytest.mark.parametrize("table", INGESTION_TABLES)
def test_unset_context_sees_nothing(engine: Engine, a: Seeded, table: str) -> None:
    with engine.connect() as conn, conn.begin():  # never called set_config at all
        assert _count(conn, table) == 0


@pytest.mark.parametrize("table", INGESTION_TABLES)
def test_org_context_sees_only_its_own_rows(
    engine: Engine, a: Seeded, b: Seeded, table: str
) -> None:
    with role_tx(engine, a.scope.org_id) as conn:
        assert _count(conn, table, a.scope.org_id) >= 1
        assert _count(conn, table, b.scope.org_id) == 0
        # every visible row is org A's
        assert _count(conn, table) == _count(conn, table, a.scope.org_id)


# (table, inserter(conn, foreign_seeded)) building a row that names ANOTHER org's ids.
def _insert_foreign(table: str, conn: Connection, foreign: Seeded) -> None:
    s = foreign.scope
    if table == "ingestion_jobs":
        insert_job(conn, s)
    elif table == "ingestion_job_events":
        conn.execute(
            text(EVENT_INSERT),
            {
                "j": foreign.job,
                "o": s.org_id,
                "w": s.workspace_id,
                "d": s.document_id,
                "v": s.version_id,
            },
        )
    elif table == "knowledge_revisions":
        insert_revision(conn, s)
    elif table == "ingestion_checkpoints":
        conn.execute(
            text(
                "INSERT INTO ingestion_checkpoints (revision_id, stage, org_id, input_fingerprint)"
                " VALUES (:r, 'chunk', :o, 'fp')"
            ),
            {"r": foreign.revision, "o": s.org_id},
        )
    elif table == "document_publications":
        conn.execute(
            text(
                "INSERT INTO document_publications (document_id, org_id, workspace_id,"
                " revision_id, version_id, version_number) VALUES (:d, :o, :w, :r, :v, 1)"
            ),
            {
                "d": s.document_id,
                "o": s.org_id,
                "w": s.workspace_id,
                "r": foreign.revision,
                "v": s.version_id,
            },
        )
    else:
        insert_outbox(conn, s)


# What each role may insert at all (others fail earlier with a privilege error).
APP_INSERTS = {"ingestion_jobs", "ingestion_job_events", "outbox_events"}


@pytest.mark.parametrize("table", INGESTION_TABLES)
def test_insert_naming_another_org_is_rejected(
    engine: Engine, a: Seeded, b: Seeded, table: str, request: pytest.FixtureRequest
) -> None:
    is_app = request.node.callspec.params["engine"] == "app"
    expected = "permission denied" if is_app and table not in APP_INSERTS else "row-level security"
    with pytest.raises(DBAPIError) as exc, role_tx(engine, a.scope.org_id) as conn:
        _insert_foreign(table, conn, b)
    assert sqlstate(exc.value) == "42501"
    assert expected in str(exc.value.orig)


@pytest.mark.parametrize("table", INGESTION_TABLES)
def test_insert_without_context_is_rejected(
    engine: Engine, a: Seeded, table: str, request: pytest.FixtureRequest
) -> None:
    is_app = request.node.callspec.params["engine"] == "app"
    expected = "permission denied" if is_app and table not in APP_INSERTS else "row-level security"
    with pytest.raises(DBAPIError) as exc, role_tx(engine, None) as conn:
        _insert_foreign(table, conn, a)
    assert sqlstate(exc.value) == "42501"
    assert expected in str(exc.value.orig)


UPDATES = {
    "ingestion_jobs": "UPDATE ingestion_jobs SET state = 'cancelled', updated_at = now()",
    "knowledge_revisions": "UPDATE knowledge_revisions SET state = 'failed'",
    "ingestion_checkpoints": "UPDATE ingestion_checkpoints SET state = 'failed'",
    "document_publications": "UPDATE document_publications SET reconciled_at = now()",
}


@pytest.mark.parametrize("table", UPDATES)
def test_worker_cannot_update_another_orgs_rows(
    worker_engine: Engine, a: Seeded, b: Seeded, admin_engine: Engine, table: str
) -> None:
    where = " WHERE org_id = :b"
    with role_tx(worker_engine, a.scope.org_id) as conn:
        assert conn.execute(text(UPDATES[table] + where), {"b": b.scope.org_id}).rowcount == 0
    with role_tx(worker_engine, None) as conn:
        assert conn.execute(text(UPDATES[table])).rowcount == 0
    with admin_engine.connect() as conn:
        # org B's rows are untouched
        assert (
            scalar(
                conn,
                "SELECT count(*) FROM ingestion_jobs WHERE org_id = :b AND state = 'cancelled'",
                b=b.scope.org_id,
            )
            == 0
        )


def test_worker_updates_its_own_orgs_rows(worker_engine: Engine, a: Seeded) -> None:
    with role_tx(worker_engine, a.scope.org_id) as conn:
        res = conn.execute(
            text("UPDATE ingestion_jobs SET state = 'cancelled', updated_at = now() WHERE id = :j"),
            {"j": a.job},
        )
        assert res.rowcount == 1
        # An UPDATE may not move a row to another org (the column is not even updatable).
    with pytest.raises(DBAPIError) as exc, role_tx(worker_engine, a.scope.org_id) as conn:
        conn.execute(
            text("UPDATE ingestion_jobs SET org_id = :o WHERE id = :j"),
            {"o": uuid.uuid4(), "j": a.job},
        )
    assert sqlstate(exc.value) == "42501"


def test_app_role_cannot_update_ingestion_tables_at_all(app_engine: Engine, a: Seeded) -> None:
    for table, column, value in (
        ("ingestion_jobs", "state", "'cancelled'"),
        ("ingestion_job_events", "detail", "'{}'::jsonb"),
        ("outbox_events", "attempts", "9"),
        ("ingestion_checkpoints", "state", "'failed'"),
        ("knowledge_revisions", "state", "'failed'"),
        ("document_publications", "reconciled_at", "now()"),
    ):
        with pytest.raises(DBAPIError) as exc, role_tx(app_engine, a.scope.org_id) as conn:
            conn.execute(text(f"UPDATE {table} SET {column} = {value}"))
        assert sqlstate(exc.value) == "42501", table
        assert "permission denied" in str(exc.value.orig), table


# -- the worker's read access to the rest of the schema is RLS-bound too ---------------------


@pytest.mark.parametrize("table", ["documents", "document_versions", "workspaces", "organizations"])
def test_worker_reads_context_tables_only_for_its_org(
    worker_engine: Engine, a: Seeded, b: Seeded, table: str
) -> None:
    column = "id" if table == "organizations" else "org_id"
    with role_tx(worker_engine, None) as conn:
        assert _count_col(conn, table, column) == 0
    with role_tx(worker_engine, a.scope.org_id) as conn:
        assert _count_col(conn, table, column, a.scope.org_id) >= 1
        assert _count_col(conn, table, column, b.scope.org_id) == 0


def _count_col(conn: Connection, table: str, column: str, org_id: uuid.UUID | None = None) -> int:
    if org_id is None:
        return int(scalar(conn, f"SELECT count(*) FROM {table}"))
    return int(scalar(conn, f"SELECT count(*) FROM {table} WHERE {column} = :o", o=org_id))


@pytest.mark.parametrize(
    "table",
    [
        "users",
        "workspace_memberships",
        "resource_grants",
        "upload_sessions",
        "audit_events",  # INSERT only: it must not be able to read audit history
    ],
)
def test_worker_has_no_access_to_other_tables(worker_engine: Engine, a: Seeded, table: str) -> None:
    with pytest.raises(DBAPIError) as exc, role_tx(worker_engine, a.scope.org_id) as conn:
        conn.execute(text(f"SELECT count(*) FROM {table}"))
    assert sqlstate(exc.value) == "42501"


def test_worker_sees_organization_memberships_only_as_far_as_the_organizations_policy_needs(
    worker_engine: Engine, a: Seeded, b: Seeded
) -> None:
    """Column-limited (org_id, user_id), RLS-bound: never ``role``, never another org."""
    with pytest.raises(DBAPIError) as exc, role_tx(worker_engine, a.scope.org_id) as conn:
        conn.execute(text("SELECT role FROM organization_memberships"))
    assert sqlstate(exc.value) == "42501"
    with role_tx(worker_engine, None) as conn:
        assert scalar(conn, "SELECT count(user_id) FROM organization_memberships") == 0
    with role_tx(worker_engine, a.scope.org_id) as conn:
        assert scalar(conn, "SELECT count(user_id) FROM organization_memberships") >= 1
        assert (
            scalar(
                conn,
                "SELECT count(*) FROM organization_memberships WHERE org_id = :o",
                o=b.scope.org_id,
            )
            == 0
        )


def test_worker_may_write_audit_events_for_its_org_only(
    worker_engine: Engine, admin_engine: Engine, a: Seeded, b: Seeded
) -> None:
    insert = (
        "INSERT INTO audit_events (id, org_id, event_type, target_type, target_id, outcome)"
        " VALUES (:i, :o, 'document.version.indexed', 'document', :t, 'success')"
    )
    own: dict[str, Any] = {"i": uuid.uuid4(), "o": a.scope.org_id, "t": str(a.scope.document_id)}
    with role_tx(worker_engine, a.scope.org_id) as conn:
        conn.execute(text(insert), own)
    foreign = {"i": uuid.uuid4(), "o": b.scope.org_id, "t": str(b.scope.document_id)}
    with pytest.raises(DBAPIError) as exc, role_tx(worker_engine, a.scope.org_id) as conn:
        conn.execute(text(insert), foreign)
    assert "row-level security" in str(exc.value.orig)
    # ... and, like everyone, it cannot rewrite or delete the audit log.
    for statement in ("UPDATE audit_events SET outcome = 'error'", "DELETE FROM audit_events"):
        with pytest.raises(DBAPIError) as exc2, role_tx(worker_engine, a.scope.org_id) as conn:
            conn.execute(text(statement))
        assert sqlstate(exc2.value) == "42501"
    with admin_engine.connect() as conn:
        assert scalar(conn, "SELECT count(*) FROM audit_events WHERE id = :i", i=own["i"]) == 1
