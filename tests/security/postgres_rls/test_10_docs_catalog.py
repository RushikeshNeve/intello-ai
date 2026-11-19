"""Catalog truth for the M02/M03 tables and the worker role: the grant registry is not a guess.

``rls_docs_world.GRANTS`` drives the behavioural tests in the later files; this file proves it
matches what PostgreSQL actually grants (table and column level), and that the worker role is
as constrained as the app role.
"""

from __future__ import annotations

import pytest
from rls_docs_world import CASE_IDS, CASES, DOC_TABLES, GRANTS, ROLES
from sqlalchemy import Engine, text

ALL_PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def _role_name(role: str, app_role: str, worker_role: str) -> str:
    return app_role if role == "app" else worker_role


def _columns(admin: Engine, table: str) -> list[str]:
    with admin.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = :t ORDER BY ordinal_position"
                ),
                {"t": table},
            ).scalars()
        )


@pytest.mark.parametrize(("role", "table"), CASES, ids=CASE_IDS)
def test_registry_matches_catalog_table_and_column_privileges(
    admin_engine: Engine, migrated: None, app_role: str, worker_role: str, role: str, table: str
) -> None:
    name = _role_name(role, app_role, worker_role)
    grant = GRANTS[role][table]
    columns = _columns(admin_engine, table)
    with admin_engine.connect() as conn:

        def has(priv: str) -> bool:
            return bool(
                conn.execute(
                    text("SELECT has_table_privilege(:r, CAST(:t AS regclass), :p)"),
                    {"r": name, "t": f"public.{table}", "p": priv},
                ).scalar_one()
            )

        assert has("SELECT") is grant.select
        assert has("INSERT") is grant.insert
        # No role may DELETE, TRUNCATE (bypasses RLS), REFERENCES (FK existence probe) or TRIGGER.
        for forbidden in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert has(forbidden) is False, f"{role} holds {forbidden} on {table}"
        # UPDATE is column-level only: never table-wide.
        assert has("UPDATE") is False
        updatable = {
            c
            for c in columns
            if conn.execute(
                text("SELECT has_column_privilege(:r, CAST(:t AS regclass), :c, 'UPDATE')"),
                {"r": name, "t": f"public.{table}", "c": c},
            ).scalar_one()
        }
    assert updatable == set(grant.update), (role, table)


@pytest.mark.parametrize("role", ROLES)
def test_no_role_can_update_identity_or_tenancy_columns(role: str) -> None:
    """Ids, org/workspace scoping and creation stamps are never updatable by either role."""
    frozen = {"id", "org_id", "workspace_id", "created_by", "created_at", "seq", "document_id"}
    for table in DOC_TABLES:
        assert not (set(GRANTS[role][table].update) & frozen), (role, table)


def test_worker_role_attributes(worker_engine: Engine, worker_role: str) -> None:
    with worker_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_user, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb,"
                " rolreplication FROM pg_roles WHERE rolname = current_user"
            )
        ).one()
    assert tuple(row) == (worker_role, False, False, False, False, False)


def test_worker_role_is_not_member_of_any_role(admin_engine: Engine, worker_role: str) -> None:
    with admin_engine.connect() as conn:
        granted = list(
            conn.execute(
                text(
                    "SELECT pg_get_userbyid(m.roleid) FROM pg_auth_members m"
                    " WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = :r)"
                ),
                {"r": worker_role},
            ).scalars()
        )
    assert granted == []


@pytest.mark.parametrize("table", DOC_TABLES)
def test_worker_does_not_own_tables(
    admin_engine: Engine, migrated: None, worker_role: str, table: str
) -> None:
    with admin_engine.connect() as conn:
        owner, member = conn.execute(
            text(
                "SELECT pg_get_userbyid(c.relowner), pg_has_role(:r, c.relowner, 'MEMBER')"
                " FROM pg_class c WHERE c.oid = CAST(:t AS regclass)"
            ),
            {"r": worker_role, "t": f"public.{table}"},
        ).one()
    assert owner != worker_role
    assert member is False


def test_worker_reachable_relations_are_exactly_the_documented_set(
    admin_engine: Engine, migrated: None, worker_role: str
) -> None:
    """Any privilege (table or column level) the worker holds on a public relation."""
    expected = {
        # read context once app.current_org_id is set
        "documents",
        "document_versions",
        "workspaces",
        "organizations",
        "organization_memberships",  # column-level SELECT (org_id, user_id) for the org policy
        "audit_events",  # INSERT only (document.version.indexed)
        # ingestion state
        "ingestion_jobs",
        "ingestion_job_events",
        "ingestion_checkpoints",
        "knowledge_revisions",
        "document_publications",
        "outbox_events",
    }
    with admin_engine.connect() as conn:
        reachable = set(
            conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = 'public' AND c.relkind IN ('r','v','m','p','f')"
                    " AND (has_any_column_privilege(:r, c.oid, 'SELECT')"
                    "   OR has_any_column_privilege(:r, c.oid, 'INSERT')"
                    "   OR has_any_column_privilege(:r, c.oid, 'UPDATE')"
                    "   OR has_any_column_privilege(:r, c.oid, 'REFERENCES')"
                    "   OR has_table_privilege(:r, c.oid, 'DELETE')"
                    "   OR has_table_privilege(:r, c.oid, 'TRUNCATE')"
                    "   OR has_table_privilege(:r, c.oid, 'TRIGGER'))"
                ),
                {"r": worker_role},
            ).scalars()
        )
    assert reachable == expected


def test_worker_membership_columns_exclude_role(
    admin_engine: Engine, migrated: None, worker_role: str
) -> None:
    columns = _columns(admin_engine, "organization_memberships")
    with admin_engine.connect() as conn:
        cols = {
            c
            for c in columns
            if conn.execute(
                text(
                    "SELECT has_column_privilege(:r, 'public.organization_memberships'::regclass,"
                    " :c, 'SELECT')"
                ),
                {"r": worker_role, "c": c},
            ).scalar_one()
        }
    assert cols == {"org_id", "user_id"}


def test_worker_audit_events_is_insert_only(
    admin_engine: Engine, migrated: None, worker_role: str
) -> None:
    columns = _columns(admin_engine, "audit_events")
    with admin_engine.connect() as conn:
        held = {
            p
            for p in ALL_PRIVS
            if conn.execute(
                text("SELECT has_table_privilege(:r, 'public.audit_events'::regclass, :p)"),
                {"r": worker_role, "p": p},
            ).scalar_one()
        }
        update_cols = [
            c
            for c in columns
            if conn.execute(
                text(
                    "SELECT has_column_privilege(:r, 'public.audit_events'::regclass, :c, 'UPDATE')"
                ),
                {"r": worker_role, "c": c},
            ).scalar_one()
        ]
    assert held == {"INSERT"}
    assert update_cols == []


@pytest.mark.parametrize("table", DOC_TABLES)
def test_every_granted_command_has_a_policy_and_nothing_else(
    admin_engine: Engine, migrated: None, table: str
) -> None:
    """Any command either role may issue is covered by a policy; commands nobody is granted
    (DELETE everywhere, UPDATE on sources/events/outbox) have none: default deny as defence in
    depth, proven by the probe-grant tests in test_12."""
    with admin_engine.connect() as conn:
        cmds = set(
            conn.execute(
                text("SELECT cmd FROM pg_policies WHERE schemaname = 'public' AND tablename = :t"),
                {"t": table},
            ).scalars()
        )
    needed = {"SELECT"}
    if any(GRANTS[r][table].insert for r in ROLES):
        needed.add("INSERT")
    if any(GRANTS[r][table].update for r in ROLES):
        needed.add("UPDATE")
    assert needed == cmds, (table, cmds)


@pytest.mark.parametrize("table", DOC_TABLES)
def test_policies_bind_every_role_and_use_the_org_guc(
    admin_engine: Engine, migrated: None, table: str
) -> None:
    """No ``TO`` clause (``roles = {public}``): the policy binds app, worker and any future role."""
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT policyname, roles::text, qual, with_check FROM pg_policies"
                " WHERE schemaname = 'public' AND tablename = :t"
            ),
            {"t": table},
        ).all()
    assert rows
    for name, roles, qual, check in rows:
        assert roles == "{public}", name
        expr = " ".join((qual or check or "").lower().split())
        assert "app.current_org_id" in expr and "nullif" in expr, (name, expr)


# ruff: noqa: PT018


# Every foreign key of the M02/M03 tables, as (columns, referenced table, referenced columns).
# The composite ones (two columns) are what pin a child to the SAME org / workspace / document as
# its parent: removing or weakening any of them is a tenancy hole even with intact policies.
_ORG = (("org_id",), "organizations", ("id",))
_WS = (("workspace_id", "org_id"), "workspaces", ("id", "org_id"))
_DOC = (("document_id", "workspace_id"), "documents", ("id", "workspace_id"))
_VER = (("version_id", "document_id"), "document_versions", ("id", "document_id"))


def _user(col: str) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
    return ((col,), "users", ("id",))


EXPECTED_FKS: dict[str, set[tuple[tuple[str, ...], str, tuple[str, ...]]]] = {
    "sources": {_ORG, _WS, _user("created_by")},
    "documents": {
        _ORG,
        _WS,
        _user("created_by"),
        _user("deleted_by"),
        (("source_id", "workspace_id"), "sources", ("id", "workspace_id")),
        (("latest_version_id", "id"), "document_versions", ("id", "document_id")),
    },
    "document_versions": {_ORG, _WS, _DOC, _user("created_by")},
    "upload_sessions": {_ORG, _WS, _user("created_by")},
    "ingestion_jobs": {_ORG, _WS, _DOC, _VER},
    "ingestion_job_events": {
        _ORG,
        _WS,
        _DOC,
        _VER,
        (("job_id", "org_id"), "ingestion_jobs", ("id", "org_id")),
    },
    "ingestion_checkpoints": {
        _ORG,
        (("revision_id", "org_id"), "knowledge_revisions", ("id", "org_id")),
    },
    "knowledge_revisions": {_ORG, _WS, _DOC, _VER},
    "document_publications": {
        _ORG,
        _WS,
        _DOC,
        _VER,
        (("revision_id", "document_id"), "knowledge_revisions", ("id", "document_id")),
        (("previous_revision_id", "document_id"), "knowledge_revisions", ("id", "document_id")),
    },
    "outbox_events": {_ORG},
}


@pytest.mark.parametrize("table", DOC_TABLES)
def test_foreign_keys_are_exactly_the_pinning_set(
    admin_engine: Engine, migrated: None, table: str
) -> None:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.conname,"
                " (SELECT array_agg(a.attname ORDER BY k.ord) FROM unnest(c.conkey)"
                "    WITH ORDINALITY k(attnum, ord)"
                "    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum),"
                " c.confrelid::regclass::text,"
                " (SELECT array_agg(a.attname ORDER BY k.ord) FROM unnest(c.confkey)"
                "    WITH ORDINALITY k(attnum, ord)"
                "    JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum)"
                " FROM pg_constraint c WHERE c.contype = 'f'"
                " AND c.conrelid = CAST(:t AS regclass)"
            ),
            {"t": f"public.{table}"},
        ).all()
    actual = {(tuple(cols), ref, tuple(refcols)) for _name, cols, ref, refcols in rows}
    assert actual == EXPECTED_FKS[table], (
        f"{table}: missing {EXPECTED_FKS[table] - actual}; extra {actual - EXPECTED_FKS[table]}"
    )
