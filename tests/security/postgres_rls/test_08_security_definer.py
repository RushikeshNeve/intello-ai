"""SECURITY DEFINER functions: pinned search_path, not callable by the app role, not abusable."""

from __future__ import annotations

import uuid

import pytest
from rls_world import World, admin_count, app_tx, expect_denied
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError


def _definers(admin: Engine) -> list[tuple[str, list[str] | None, str]]:
    with admin.connect() as conn:
        return [
            (r.proname, r.proconfig, r.owner)
            for r in conn.execute(
                text(
                    "SELECT p.proname, p.proconfig, pg_get_userbyid(p.proowner) AS owner"
                    " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                    " WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
                )
            )
        ]


# M03 (migration 0005, contract §4): the worker's only way to look across orgs. Executable by the
# worker role alone (see tests/integration/m03_foundation/test_m03_grants.py for their depth).
M03_DEFINERS = [
    "ingest_claim_job",
    "ingest_reap_expired_leases",
    "knowledge_next_reconcile_batch",
    "outbox_fetch_batch",
    "outbox_mark_dispatched",
    "outbox_mark_failed",
]
# M03.03.04 (migration 0006): the one definer the APP role may call (a user's "retry"); it only
# acts on the caller's own org (app.current_org_id) and PUBLIC still may not execute it.
# M04 (migration 0007): worker-only definers (see test_17_chat_tables.py for their depth) and the one
# app-role definer, a creator's own "cancel run" (acts on app.current_org_id + app.current_user_id).
M04_WORKER_DEFINERS = ["chat_claim_run", "chat_reap_expired_runs", "chat_readable_workspaces"]
# M06 (migration 0009): the scheduler tick (worker) and the unauthenticated webhook's source
# lookup (app; returns org + sealed secret by source id). Depth: test_18_connector_tables.py.
M06_WORKER_DEFINERS = ["connector_enqueue_due_syncs"]
# M07 (migration 0010): worker-only maintenance/claim definers; the app role may call the memory
# standing check (also used by the memories policies), the evidence invalidation (acts only on the
# context org) and a creator's own research cancel. Depth: test_19_m07_tables.py.
M07_WORKER_DEFINERS = ["memory_sweep", "research_claim_session", "research_reap_expired"]
# Migration 0015b: the embedding-cache eviction sweep lists orgs with cache rows (worker only).
M11_WORKER_DEFINERS = ["embedding_cache_orgs"]
# Migration 0020 (M13.02): expired-capture sweep across orgs (worker only).
M13_WORKER_DEFINERS = ["ai_run_captures_sweep"]
APP_DEFINERS = [
    "ingest_requeue_job",
    "chat_cancel_run",
    "connector_webhook_lookup",
    "memory_ws_access",
    "memory_invalidate_evidence",
    "research_cancel",
    # Migration 0011: sign-in consumption of pre-approvals; admin-only verified-user lookup.
    "preapproval_consume",
    "preapproval_find_user",
    # Migration 0016: seeds the global prompt default from the repo file (app + worker).
    "prompt_seed_default",
    # Migration 0018: blue/green cutover + rollback; owner/admin of the context org only.
    "reindex_cutover",
    "reindex_rollback",
    # Migration 0024 (M14.02): workspace admin releases a quarantined version (context org only).
    "ingest_release_quarantine",
]


def test_expected_definer_inventory(admin_engine: Engine, migrated: None) -> None:
    assert sorted(name for name, _, _ in _definers(admin_engine)) == sorted(
        [
            "ukos_assert_org_has_owner",
            *M03_DEFINERS,
            *M04_WORKER_DEFINERS,
            *M06_WORKER_DEFINERS,
            *M07_WORKER_DEFINERS,
            *M11_WORKER_DEFINERS,
            *M13_WORKER_DEFINERS,
            *APP_DEFINERS,
        ]
    )


def test_definers_pin_search_path(admin_engine: Engine, migrated: None) -> None:
    for name, config, _owner in _definers(admin_engine):
        paths = [c for c in (config or []) if c.startswith("search_path=")]
        assert paths, f"{name}: SECURITY DEFINER without pinned search_path"
        entries = [e.strip() for e in paths[0].split("=", 1)[1].split(",")]
        assert entries[0] == "pg_catalog", name
        assert "$user" not in entries and '"$user"' not in entries, name


def test_definers_not_executable_by_app_role_or_public(
    admin_engine: Engine, migrated: None, app_role: str
) -> None:
    with admin_engine.connect() as conn:
        for name, _, _ in _definers(admin_engine):
            if name in APP_DEFINERS:
                continue  # executable by the app role by design; see the next test
            # Overload-agnostic: every SECURITY DEFINER function of that name, by oid.
            for _oid, can_execute, acl in conn.execute(
                text(
                    "SELECT p.oid, has_function_privilege(:r, p.oid, 'EXECUTE'), p.proacl::text"
                    " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                    " WHERE p.prosecdef AND n.nspname = 'public' AND p.proname = :n"
                ),
                {"r": app_role, "n": name},
            ):
                assert can_execute is False, name
                # NULL acl = default = PUBLIC may EXECUTE; a PUBLIC entry is "=X/<grantor>".
                assert acl is not None, f"{name}: default ACL grants PUBLIC EXECUTE"
                items = acl.strip("{}").split(",")
                assert not any(i.startswith("=") for i in items), f"{name}: PUBLIC entry in {acl}"


def test_app_definer_is_app_only_and_not_public(
    admin_engine: Engine, migrated: None, app_role: str
) -> None:
    with admin_engine.connect() as conn:
        for name in APP_DEFINERS:
            rows = conn.execute(
                text(
                    "SELECT has_function_privilege(:r, p.oid, 'EXECUTE'), p.proacl::text"
                    " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                    " WHERE p.prosecdef AND n.nspname = 'public' AND p.proname = :n"
                ),
                {"r": app_role, "n": name},
            ).all()
            assert rows, name
            for can_execute, acl in rows:
                assert can_execute is True, name
                assert acl is not None
                assert not any(i.startswith("=") for i in acl.strip("{}").split(",")), acl


def test_app_role_cannot_call_definer_directly(app_engine: Engine, world: World) -> None:
    expect_denied(
        app_engine,
        "SELECT public.ukos_assert_org_has_owner()",
        match="permission denied",
        user=world.a.owner_id,
        org=world.b.org_id,
    )


def test_app_role_cannot_replace_definer(app_engine: Engine) -> None:
    expect_denied(
        app_engine,
        "CREATE OR REPLACE FUNCTION public.ukos_assert_org_has_owner() RETURNS trigger"
        " LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$",
    )
    expect_denied(app_engine, "ALTER FUNCTION public.ukos_assert_org_has_owner() RESET ALL")


def test_definer_trigger_still_enforces_owner_with_shadow_temp_tables(
    app_engine: Engine, world: World
) -> None:
    """pg_temp comes first for relations when not listed in search_path. Empty temp
    'organizations' / 'organization_memberships' must not fool the definer (its body uses
    schema-qualified names): removing A's only owner still fails at COMMIT with 23514."""
    a = world.a
    with app_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                text(
                    "SELECT set_config('app.current_user_id', :u, true),"
                    " set_config('app.current_org_id', :o, true)"
                ),
                {"u": str(a.owner_id), "o": str(a.org_id)},
            )
            conn.execute(text("CREATE TEMP TABLE organizations (id uuid, deleted_at timestamptz)"))
            conn.execute(
                text("CREATE TEMP TABLE organization_memberships (org_id uuid, role text)")
            )
            r = conn.execute(
                text("UPDATE public.organization_memberships SET role = 'admin'"
                     " WHERE org_id = :o AND user_id = :u"),
                {"o": a.org_id, "u": a.owner_id},
            )  # fmt: skip
            assert r.rowcount == 1
            with pytest.raises(DBAPIError) as info:
                trans.commit()
            assert getattr(info.value.orig, "sqlstate", None) == "23514"
            assert "last_owner" in str(info.value.orig)
        finally:
            if trans.is_active:
                trans.rollback()


def test_definer_trigger_is_not_a_cross_org_oracle(
    app_engine: Engine, admin_engine: Engine, world: World
) -> None:
    """From A, attempts on B's owner never reach the definer: 0 rows, commit succeeds, B intact."""
    b = world.b
    with app_engine.connect() as conn, conn.begin():
        conn.execute(
            text(
                "SELECT set_config('app.current_user_id', :u, true),"
                " set_config('app.current_org_id', :o, true)"
            ),
            {"u": str(world.a.owner_id), "o": str(world.a.org_id)},
        )
        for sql in (
            "DELETE FROM organization_memberships WHERE org_id = :b",
            "UPDATE organization_memberships SET role = 'member' WHERE org_id = :b",
            "UPDATE organizations SET deleted_at = NULL WHERE id = :b",
        ):
            assert conn.execute(text(sql), {"b": b.org_id}).rowcount == 0, sql
        # committed without error
    assert (
        admin_count(
            admin_engine,
            "SELECT count(*) FROM organization_memberships WHERE org_id = :b AND role = 'owner'",
            {"b": b.org_id},
        )
        == 1
    )


def test_owner_invariant_holds_for_org_insert_without_owner(
    app_engine: Engine, world: World
) -> None:
    """The definer sees through RLS correctly: creating an org with no owner fails at commit."""
    with app_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                text("SELECT set_config('app.current_user_id', :u, true)"),
                {"u": str(world.outsider_id)},
            )
            conn.execute(
                text("INSERT INTO organizations (id, slug, name, created_by)"
                     " VALUES (:id, :s, 'x', :u)"),
                {"id": uuid.uuid4(), "s": f"noowner-{uuid.uuid4().hex[:10]}",
                 "u": world.outsider_id},
            )  # fmt: skip
            with pytest.raises(DBAPIError) as info:
                trans.commit()
            assert getattr(info.value.orig, "sqlstate", None) == "23514"
        finally:
            if trans.is_active:
                trans.rollback()


def test_app_tx_helper_rolls_back(app_engine: Engine, admin_engine: Engine, world: World) -> None:
    """Sanity for the harness itself: attacks never persist."""
    marker = f"harness-{uuid.uuid4().hex[:8]}"
    with app_tx(app_engine, user=world.a.owner_id, org=world.a.org_id) as conn:
        conn.execute(
            text("UPDATE workspaces SET name = :n WHERE id = :w"),
            {"n": marker, "w": world.a.workspace_id},
        )
    assert (
        admin_count(admin_engine, "SELECT count(*) FROM workspaces WHERE name = :n", {"n": marker})
        == 0
    )
