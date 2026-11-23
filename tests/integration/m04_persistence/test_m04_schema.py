"""Migration 0007: round trip, model parity, constraints, triggers, grants, definer hygiene."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from compose_support import ComposeProject
from m04_support import ChatWorld, constraint, scalar, sqlstate
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.models import ORG_SCOPED_TABLES, RLS_TABLES, Base
from app.modules.ai.models_db import CHAT_TABLES

M04_HEAD = "0007_chat_and_ai_policy"
M05_HEAD = "0008_graph"
M06_HEAD = "0009_connectors"  # M06 sits on top of 0008
M07_HEAD = "0010_memory_research"
PREAPPROVALS_HEAD = "0011_member_preapprovals"  # member pre-approvals on top of 0010
# M11/M12 chain on top of 0011 (oldest first; 0015 landed after 0018).
NEXT_PHASE_CHAIN = (
    "0012_usage_events",
    "0013_embedding_cache",
    "0014_chat_stage_event",
    "0016_prompt_versions",
    "0017_evals",
    "0018_reindex",
    "0015_usage_limits",
    "0015b_cache_evict_orgs",
    "0019a_prompt_gate",
    "0019b_workspace_styles",
    "0019c_shadow_runs",
    "0020_ai_run_captures",
    "0021_answer_feedback",
)
HEAD = NEXT_PHASE_CHAIN[-1]
PREVIOUS = "0006_ingestion_requeue"
FUNCTIONS = {
    "chat_claim_run",
    "chat_reap_expired_runs",
    "chat_cancel_run",
    "chat_readable_workspaces",
    "ukos_chat_messages_append_only",
    "ukos_chat_run_events_append_only",
}


def _tables(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )


def _functions(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text("SELECT proname FROM pg_proc WHERE proname = ANY(:n)"),
                {"n": sorted(FUNCTIONS)},
            ).scalars()
        )


def test_head_is_0007(alembic_config: Config) -> None:
    script = ScriptDirectory.from_config(alembic_config)
    assert script.get_heads() == [HEAD]
    assert script.get_revision(M04_HEAD).down_revision == PREVIOUS
    assert script.get_revision(M05_HEAD).down_revision == M04_HEAD
    assert script.get_revision(M06_HEAD).down_revision == M05_HEAD
    assert script.get_revision(M07_HEAD).down_revision == M06_HEAD
    for rev, down in zip(NEXT_PHASE_CHAIN, (PREAPPROVALS_HEAD, *NEXT_PHASE_CHAIN[:-1])):
        assert script.get_revision(rev).down_revision == down
    assert script.get_revision(PREAPPROVALS_HEAD).down_revision == M07_HEAD


def test_models_are_registered_and_org_scoped() -> None:
    assert set(CHAT_TABLES) <= set(ORG_SCOPED_TABLES)
    assert set(Base.metadata.tables) == set(RLS_TABLES)


def test_models_match_migration(admin_engine: Engine, migrated: None) -> None:
    with admin_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, Base.metadata) == []


def test_round_trip_restores_the_outbox_check_and_drops_everything(
    alembic_config: Config, admin_engine: Engine, world: ChatWorld, migrated: None
) -> None:
    assert set(CHAT_TABLES) <= _tables(admin_engine)
    assert _functions(admin_engine) == FUNCTIONS
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO outbox_events (org_id, aggregate_type, aggregate_id, event_type)"
                " VALUES (:o, 'chat_run', :a, 'chat.run.queued')"
            ),
            {"o": world.org, "a": uuid.uuid4()},
        )
    command.downgrade(alembic_config, PREVIOUS)
    try:
        assert _tables(admin_engine) & set(CHAT_TABLES) == set()
        assert _functions(admin_engine) == set()
        with admin_engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM outbox_events WHERE event_type = 'chat.run.queued'")
                ).scalar_one()
                == 0
            )
            definition = conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE conname = 'ck_outbox_events_type'"
                )
            ).scalar_one()
        assert "chat.run.queued" not in definition
        with pytest.raises(IntegrityError), admin_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO outbox_events (org_id, aggregate_type, aggregate_id, event_type)"
                    " VALUES (:o, 'chat_run', :a, 'chat.run.queued')"
                ),
                {"o": world.org, "a": uuid.uuid4()},
            )
    finally:
        command.upgrade(alembic_config, "head")
    assert set(CHAT_TABLES) <= _tables(admin_engine)
    assert _functions(admin_engine) == FUNCTIONS
    assert scalar(admin_engine, "SELECT version_num FROM alembic_version") == HEAD


def test_downgrade_leaves_no_chat_catalog_objects(
    alembic_config: Config, admin_engine: Engine, migrated: None
) -> None:
    command.downgrade(alembic_config, PREVIOUS)
    try:
        with admin_engine.connect() as conn:
            leftovers = conn.execute(
                text(
                    "SELECT 'trigger:' || tgname FROM pg_trigger"
                    " WHERE NOT tgisinternal AND tgname LIKE 'chat\\_%'"
                    " UNION ALL SELECT 'index:' || indexname FROM pg_indexes"
                    " WHERE indexname LIKE 'ix\\_chat\\_%'"
                    " UNION ALL SELECT 'policy:' || policyname FROM pg_policies"
                    " WHERE tablename LIKE 'chat\\_%' OR tablename = 'ai_policies'"
                )
            ).all()
        assert leftovers == []
    finally:
        command.upgrade(alembic_config, "head")


# ------------------------------------------------------------------------------- constraints


def _insert_thread(
    conn: Any, w: ChatWorld, org: uuid.UUID | None = None, ws: Any = ...
) -> uuid.UUID:
    tid = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO chat_threads (id, org_id, workspace_id, created_by)"
            " VALUES (:i, :o, :w, :u)"
        ),
        {
            "i": tid,
            "o": org or w.org,
            "w": w.ws if ws is ... else ws,
            "u": w.owner_b if org == w.org_b else w.member,
        },
    )
    return tid


def _insert_run(conn: Any, w: ChatWorld, thread: uuid.UUID, **over: Any) -> uuid.UUID:
    rid, mid = uuid.uuid4(), uuid.uuid4()
    values: dict[str, Any] = {
        "id": rid,
        "o": w.org,
        "t": thread,
        "m": mid,
        "u": w.member,
        "st": "queued",
        "k": uuid.uuid4().hex,
        "owner": None,
        "fin": None,
        "ec": None,
    }
    values.update(over)
    conn.execute(
        text(
            "INSERT INTO chat_runs (id, org_id, thread_id, user_message_id, created_by, state,"
            " idempotency_key, lease_owner, finished_at, error_code)"
            " VALUES (:id, :o, :t, :m, :u, :st, :k, :owner,"
            " CASE WHEN :fin THEN now() END, :ec)"
        ),
        {**values, "fin": bool(values["fin"])},
    )
    conn.execute(
        text(
            "INSERT INTO chat_messages (id, org_id, thread_id, role, content, run_id)"
            " VALUES (:m, :o, :t, 'user', 'q', :id)"
        ),
        values,
    )
    return rid


def _expect(admin_engine: Engine, name: str, fn: Any) -> None:
    with pytest.raises(DBAPIError) as exc, admin_engine.begin() as conn:
        fn(conn)
    assert constraint(exc.value) == name, (constraint(exc.value), exc.value)


def test_run_state_and_lease_checks(admin_engine: Engine, world: ChatWorld) -> None:
    with admin_engine.begin() as conn:
        thread = _insert_thread(conn, world)
    _expect(
        admin_engine, "ck_chat_runs_state", lambda c: _insert_run(c, world, thread, st="paused")
    )
    _expect(
        admin_engine, "ck_chat_runs_lease", lambda c: _insert_run(c, world, thread, st="running")
    )
    _expect(
        admin_engine, "ck_chat_runs_lease", lambda c: _insert_run(c, world, thread, owner="w")
    )  # queued with an owner
    _expect(
        admin_engine,
        "ck_chat_runs_finished",
        lambda c: _insert_run(c, world, thread, st="completed"),
    )
    _expect(
        admin_engine, "ck_chat_runs_finished", lambda c: _insert_run(c, world, thread, fin=True)
    )
    _expect(
        admin_engine,
        "ck_chat_runs_error_code",
        lambda c: _insert_run(c, world, thread, st="completed", fin=True, ec="x"),
    )
    _expect(
        admin_engine, "ck_chat_runs_idempotency_key", lambda c: _insert_run(c, world, thread, k="")
    )
    _expect(
        admin_engine,
        "ck_chat_runs_idempotency_key",
        lambda c: _insert_run(c, world, thread, k="k" * 129),
    )
    with admin_engine.begin() as conn:
        _insert_run(conn, world, thread, st="failed", fin=True, ec="timeout")


def test_unique_idempotency_key_per_thread(admin_engine: Engine, world: ChatWorld) -> None:
    with admin_engine.begin() as conn:
        thread = _insert_thread(conn, world)
        _insert_run(conn, world, thread, k="dup")
    _expect(
        admin_engine,
        "uq_chat_runs_thread_id_idempotency_key",
        lambda c: _insert_run(c, world, thread, k="dup"),
    )


def test_composite_fks_pin_org_thread_and_run(admin_engine: Engine, world: ChatWorld) -> None:
    with admin_engine.begin() as conn:
        t_a = _insert_thread(conn, world)
        t_a2 = _insert_thread(conn, world)
        run_a = _insert_run(conn, world, t_a)

    # The thread's workspace belongs to org A while the row claims org B.
    _expect(
        admin_engine,
        "fk_chat_threads_workspace",
        lambda c: c.execute(
            text(
                "INSERT INTO chat_threads (id, org_id, workspace_id, created_by)"
                " VALUES (:i, :o, :w, :u)"
            ),
            {"i": uuid.uuid4(), "o": world.org_b, "w": world.ws, "u": world.owner_b},
        ),
    )
    # A message claiming org B for a thread of org A.
    _expect(
        admin_engine,
        "fk_chat_messages_thread",
        lambda c: c.execute(
            text(
                "INSERT INTO chat_messages (id, org_id, thread_id, role, content)"
                " VALUES (:i, :o, :t, 'user', 'x')"
            ),
            {"i": uuid.uuid4(), "o": world.org_b, "t": t_a},
        ),
    )
    # A run claiming org B for a thread of org A.
    _expect(
        admin_engine,
        "fk_chat_runs_thread",
        lambda c: c.execute(
            text(
                "INSERT INTO chat_runs (id, org_id, thread_id, user_message_id, created_by,"
                " idempotency_key) VALUES (:i, :o, :t, :m, :u, 'k')"
            ),
            {"i": uuid.uuid4(), "o": world.org_b, "t": t_a, "m": uuid.uuid4(), "u": world.owner_b},
        ),
    )
    # A message pointing at a run of another thread.
    _expect(
        admin_engine,
        "fk_chat_messages_run",
        lambda c: c.execute(
            text(
                "INSERT INTO chat_messages (id, org_id, thread_id, role, content, run_id)"
                " VALUES (:i, :o, :t, 'assistant', 'x', :r)"
            ),
            {"i": uuid.uuid4(), "o": world.org, "t": t_a2, "r": run_a},
        ),
    )
    # An event claiming org B for a run of org A.
    _expect(
        admin_engine,
        "fk_chat_run_events_run",
        lambda c: c.execute(
            text("INSERT INTO chat_run_events (run_id, org_id, type) VALUES (:r, :o, 'started')"),
            {"r": run_a, "o": world.org_b},
        ),
    )
    # A run whose user message is missing fails at COMMIT (deferred FK).
    with pytest.raises(DBAPIError) as exc, admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO chat_runs (id, org_id, thread_id, user_message_id, created_by,"
                " idempotency_key) VALUES (:i, :o, :t, :m, :u, 'orphan')"
            ),
            {"i": uuid.uuid4(), "o": world.org, "t": t_a, "m": uuid.uuid4(), "u": world.member},
        )
    assert constraint(exc.value) == "fk_chat_runs_user_message"


def test_message_and_event_checks(admin_engine: Engine, world: ChatWorld) -> None:
    with admin_engine.begin() as conn:
        thread = _insert_thread(conn, world)
        run = _insert_run(conn, world, thread)

    def msg(role: str, content: str, citations: str = "[]") -> Any:
        return lambda c: c.execute(
            text(
                "INSERT INTO chat_messages (id, org_id, thread_id, role, content, citations)"
                " VALUES (:i, :o, :t, :r, :c, CAST(:ci AS jsonb))"
            ),
            {
                "i": uuid.uuid4(),
                "o": world.org,
                "t": thread,
                "r": role,
                "c": content,
                "ci": citations,
            },
        )

    _expect(admin_engine, "ck_chat_messages_role", msg("system", "x"))
    _expect(admin_engine, "ck_chat_messages_content", msg("user", ""))
    _expect(admin_engine, "ck_chat_messages_content", msg("user", "x" * 100_001))
    _expect(admin_engine, "ck_chat_messages_citations", msg("assistant", "x", "{}"))
    _expect(
        admin_engine,
        "ck_chat_run_events_type",
        lambda c: c.execute(
            text("INSERT INTO chat_run_events (run_id, org_id, type) VALUES (:r, :o, 'token')"),
            {"r": run, "o": world.org},
        ),
    )
    _expect(
        admin_engine,
        "ck_chat_run_events_data",
        lambda c: c.execute(
            text(
                "INSERT INTO chat_run_events (run_id, org_id, type, data)"
                " VALUES (:r, :o, 'delta', CAST(:d AS jsonb))"
            ),
            {"r": run, "o": world.org, "d": '{"t": "' + "x" * 70_000 + '"}'},
        ),
    )
    _expect(
        admin_engine,
        "ck_chat_threads_title",
        lambda c: c.execute(
            text(
                "INSERT INTO chat_threads (id, org_id, created_by, title) VALUES (:i, :o, :u, :t)"
            ),
            {"i": uuid.uuid4(), "o": world.org, "u": world.member, "t": "t" * 201},
        ),
    )


def test_ai_policy_checks(admin_engine: Engine, world: ChatWorld) -> None:
    def put(allow: bool, providers: str) -> Any:
        return lambda c: c.execute(
            text(
                "INSERT INTO ai_policies (org_id, allow_external, allowed_providers)"
                " VALUES (:o, :a, CAST(:p AS text[]))"
            ),
            {"o": world.org_b, "a": allow, "p": providers},
        )

    _expect(admin_engine, "ck_ai_policies_providers", put(True, "{}"))
    _expect(admin_engine, "ck_ai_policies_providers", put(True, "{claude}"))
    _expect(admin_engine, "ck_ai_policies_external", put(False, "{local,openai}"))
    with admin_engine.begin() as conn:
        put(True, "{local,openai}")(conn)
        conn.execute(text("DELETE FROM ai_policies WHERE org_id = :o"), {"o": world.org_b})
        conn.execute(text("INSERT INTO ai_policies (org_id) VALUES (:o)"), {"o": world.org_b})
        row = conn.execute(
            text("SELECT allow_external, allowed_providers FROM ai_policies WHERE org_id = :o"),
            {"o": world.org_b},
        ).one()
        conn.execute(text("DELETE FROM ai_policies WHERE org_id = :o"), {"o": world.org_b})
    assert (row.allow_external, row.allowed_providers) == (False, ["local"])  # the default row


def test_outbox_accepts_chat_run_queued_only_as_added(
    admin_engine: Engine, world: ChatWorld
) -> None:
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO outbox_events (org_id, aggregate_type, aggregate_id, event_type)"
                " VALUES (:o, 'chat_run', :a, 'chat.run.queued')"
            ),
            {"o": world.org, "a": uuid.uuid4()},
        )
    _expect(
        admin_engine,
        "ck_outbox_events_type",
        lambda c: c.execute(
            text(
                "INSERT INTO outbox_events (org_id, aggregate_type, aggregate_id, event_type)"
                " VALUES (:o, 'chat_run', :a, 'chat.run.other')"
            ),
            {"o": world.org, "a": uuid.uuid4()},
        ),
    )


@pytest.mark.parametrize("table", ["chat_messages", "chat_run_events"])
def test_append_only_triggers_reject_update_delete_truncate(
    admin_engine: Engine, world: ChatWorld, table: str
) -> None:
    with admin_engine.begin() as conn:
        thread = _insert_thread(conn, world)
        run = _insert_run(conn, world, thread)
        conn.execute(
            text("INSERT INTO chat_run_events (run_id, org_id, type) VALUES (:r, :o, 'started')"),
            {"r": run, "o": world.org},
        )
    column = "content" if table == "chat_messages" else "type"
    value = "'y'" if table == "chat_messages" else "'delta'"
    for sql in (
        f"UPDATE {table} SET {column} = {value}",
        f"DELETE FROM {table}",
        f"TRUNCATE {table}",
    ):
        with pytest.raises(DBAPIError) as exc, admin_engine.begin() as conn:
            conn.execute(text(sql))
        if sql.startswith("TRUNCATE") and sqlstate(exc.value) == "0A000":
            continue  # chat_messages is referenced by chat_runs: PostgreSQL refuses even earlier
        assert sqlstate(exc.value) == "42501", sql
        assert "append-only" in str(exc.value)
    with admin_engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_trigger WHERE tgname = :n AND NOT tgisinternal"
                    " AND (tgtype & 32) <> 0"  # TRIGGER_TYPE_TRUNCATE
                ),
                {"n": f"{table}_no_truncate"},
            ).scalar_one()
            == 1
        )


# ----------------------------------------------------------------------------------- grants

ALL = {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
APP_PRIVS = {
    "ai_policies": (
        {"SELECT", "INSERT"},
        {
            "allow_external",
            "allowed_providers",
            "updated_by",
            "updated_at",
            "capture_mode",
            "capture_retention_days",
        },
    ),
    "chat_threads": ({"SELECT", "INSERT"}, {"title", "deleted_at", "updated_at"}),
    "chat_messages": ({"SELECT", "INSERT"}, set()),
    "chat_runs": ({"SELECT", "INSERT"}, set()),
    "chat_run_events": ({"SELECT"}, set()),
}
WORKER_PRIVS = {
    "ai_policies": ({"SELECT"}, set()),
    "chat_threads": ({"SELECT"}, {"updated_at"}),
    "chat_messages": ({"SELECT", "INSERT"}, set()),
    "chat_runs": (
        {"SELECT"},
        {
            "state",
            "model",
            "started_at",
            "finished_at",
            "error_code",
            "lease_owner",
            "lease_expires_at",
        },
    ),
    "chat_run_events": ({"SELECT", "INSERT"}, set()),
}


def _privs(engine: Engine, role: str, table: str) -> tuple[set[str], set[str]]:
    with engine.connect() as conn:
        held = {
            p
            for p in ALL
            if conn.execute(
                text("SELECT has_table_privilege(:r, CAST(:t AS regclass), :p)"),
                {"r": role, "t": f"public.{table}", "p": p},
            ).scalar_one()
        }
        columns = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        ).scalars()
        upd = {
            c
            for c in columns
            if conn.execute(
                text("SELECT has_column_privilege(:r, CAST(:t AS regclass), :c, 'UPDATE')"),
                {"r": role, "t": f"public.{table}", "c": c},
            ).scalar_one()
        }
    return held, upd


@pytest.mark.parametrize("table", sorted(APP_PRIVS))
def test_app_role_grants_are_exact(
    admin_engine: Engine, stack: ComposeProject, migrated: None, table: str
) -> None:
    held, upd = _privs(admin_engine, stack.env["POSTGRES_APP_USER"], table)
    assert (held - {"UPDATE"}, upd) == APP_PRIVS[table]
    assert not held & {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}


@pytest.mark.parametrize("table", sorted(WORKER_PRIVS))
def test_worker_role_grants_are_exact(
    admin_engine: Engine, stack: ComposeProject, migrated: None, table: str
) -> None:
    held, upd = _privs(admin_engine, stack.env["POSTGRES_WORKER_USER"], table)
    assert (held - {"UPDATE"}, upd) == WORKER_PRIVS[table]
    assert not held & {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}


def test_worker_cannot_touch_identity_or_fencing_columns(
    admin_engine: Engine, stack: ComposeProject, migrated: None
) -> None:
    _, upd = _privs(admin_engine, stack.env["POSTGRES_WORKER_USER"], "chat_runs")
    for column in (
        "id", "org_id", "thread_id", "user_message_id", "created_by", "idempotency_key",
        "scope_snapshot", "attempts", "lease_epoch", "created_at",
    ):  # fmt: skip
        assert column not in upd, column


# ---------------------------------------------------------------------- definer function hygiene

SIGNATURES = {
    "chat_claim_run": "worker",
    "chat_reap_expired_runs": "worker",
    "chat_readable_workspaces": "worker",
    "chat_cancel_run": "app",
}


@pytest.mark.parametrize("name", sorted(SIGNATURES))
def test_definer_functions_are_pinned_and_executable_by_one_role_only(
    admin_engine: Engine, stack: ComposeProject, migrated: None, name: str
) -> None:
    app, worker = stack.env["POSTGRES_APP_USER"], stack.env["POSTGRES_WORKER_USER"]
    with admin_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT p.oid, p.prosecdef, p.proconfig, p.proacl::text FROM pg_proc p"
                " WHERE p.proname = :n"
            ),
            {"n": name},
        ).one()
        assert row.prosecdef is True
        assert row.proconfig == ["search_path=pg_catalog, public, pg_temp"]
        can = {
            role: conn.execute(
                text("SELECT has_function_privilege(:r, :o, 'EXECUTE')"), {"r": role, "o": row.oid}
            ).scalar_one()
            for role in (app, worker)
        }
        public = conn.execute(
            text("SELECT has_function_privilege('public', :o, 'EXECUTE')"), {"o": row.oid}
        ).scalar_one()
    owner_role = app if SIGNATURES[name] == "app" else worker
    assert can == {app: owner_role == app, worker: owner_role == worker}
    assert public is False


def test_app_role_cannot_call_the_worker_functions_and_vice_versa(
    app_engine: Engine, worker_engine: Engine, migrated: None
) -> None:
    with pytest.raises(DBAPIError) as exc, app_engine.connect() as conn:
        conn.execute(text("SELECT * FROM chat_claim_run(:r, 'x', 5)"), {"r": uuid.uuid4()})
    assert sqlstate(exc.value) == "42501"
    with pytest.raises(DBAPIError) as exc2, worker_engine.connect() as conn:
        conn.execute(text("SELECT * FROM chat_cancel_run(:r)"), {"r": uuid.uuid4()})
    assert sqlstate(exc2.value) == "42501"
