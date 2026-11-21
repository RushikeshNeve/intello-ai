"""Migration 0005: round trip, worker role lifecycle, catalog cleanliness, model parity."""

from __future__ import annotations

from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from compose_support import ComposeProject
from m03_support import alembic_cfg, make_url
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.models import ORG_SCOPED_TABLES, RLS_TABLES, Base
from app.modules.ingestion.models import INGESTION_TABLES

REVISION = "0005_ingestion"
PREAPPROVALS_HEAD = "0011_member_preapprovals"  # member pre-approvals on top of 0010
# M11/M12 chain on top of 0011 (oldest first; 0015 intentionally unused).
NEXT_PHASE_CHAIN = (
    "0012_usage_events",
    "0013_embedding_cache",
    "0014_chat_stage_event",
    "0016_prompt_versions",
    "0017_evals",
    "0018_reindex",
)
HEAD = NEXT_PHASE_CHAIN[-1]
M07_HEAD = "0010_memory_research"  # M07: memory + research on top of 0009
M06_HEAD = "0009_connectors"  # M06: connectors on top of 0008
M05_HEAD = "0008_graph"  # M05: graph identity + merge review on top of 0007
M04_HEAD = "0007_chat_and_ai_policy"  # M04: chat + ai policy on top of 0006
M03_HEAD = "0006_ingestion_requeue"  # M03.03.04: ingest_requeue_job on top of 0005
PREVIOUS = "0004_upload_session_title"
PROBE = "ukos_probe_worker"
FUNCTIONS = {
    "ingest_claim_job",
    "ingest_reap_expired_leases",
    "outbox_fetch_batch",
    "outbox_mark_dispatched",
    "outbox_mark_failed",
    "knowledge_next_reconcile_batch",
    "ingest_requeue_job",
    "ukos_ingestion_job_events_append_only",
}
# (LOGIN, SUPERUSER, BYPASSRLS, CREATEROLE, CREATEDB, REPLICATION)
WORKER_ATTRIBUTES = (True, False, False, False, False, False)


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


def _role(engine: Engine, name: str) -> tuple[Any, ...] | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb,"
                " rolreplication FROM pg_roles WHERE rolname = :r"
            ),
            {"r": name},
        ).first()
    return None if row is None else tuple(row)


def test_head_is_0007_on_top_of_the_m03_chain(alembic_config: Config) -> None:
    script = ScriptDirectory.from_config(alembic_config)
    assert script.get_heads() == [HEAD]
    for rev, down in zip(NEXT_PHASE_CHAIN, (PREAPPROVALS_HEAD, *NEXT_PHASE_CHAIN[:-1])):
        assert script.get_revision(rev).down_revision == down
    assert script.get_revision(PREAPPROVALS_HEAD).down_revision == M07_HEAD
    assert script.get_revision(M07_HEAD).down_revision == M06_HEAD
    assert script.get_revision(M06_HEAD).down_revision == M05_HEAD
    assert script.get_revision(M05_HEAD).down_revision == M04_HEAD
    assert script.get_revision(M04_HEAD).down_revision == M03_HEAD
    assert script.get_revision(M03_HEAD).down_revision == REVISION
    assert script.get_revision(REVISION).down_revision == PREVIOUS


def test_models_are_registered_and_org_scoped() -> None:
    assert set(INGESTION_TABLES) <= set(ORG_SCOPED_TABLES)
    assert set(Base.metadata.tables) == set(RLS_TABLES)


def test_models_match_migration(admin_engine: Engine, migrated: None) -> None:
    """Alembic autogenerate against the migrated database finds no difference."""
    with admin_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_round_trip_keeps_the_worker_role(
    alembic_config: Config, admin_engine: Engine, stack: ComposeProject, migrated: None
) -> None:
    assert set(INGESTION_TABLES) <= _tables(admin_engine)
    assert _functions(admin_engine) == FUNCTIONS
    worker = stack.env["POSTGRES_WORKER_USER"]
    assert _role(admin_engine, worker) is not None

    command.downgrade(alembic_config, PREVIOUS)
    try:
        assert _tables(admin_engine) & set(INGESTION_TABLES) == set()
        assert _functions(admin_engine) == set()
        assert _role(admin_engine, worker) is not None  # survives, stripped of its privileges
        with admin_engine.connect() as conn:
            leftover = conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.role_table_grants"
                    " WHERE grantee = :r AND table_schema = 'public'"
                ),
                {"r": worker},
            ).scalar_one()
            policies = conn.execute(
                text("SELECT count(*) FROM pg_policies WHERE tablename = ANY(:t)"),
                {"t": list(INGESTION_TABLES)},
            ).scalar_one()
        assert leftover == 0
        assert policies == 0
        # The M01/M02 schema is untouched.
        assert {"users", "documents", "document_versions", "audit_events"} <= _tables(admin_engine)

        command.downgrade(alembic_config, "base")
        assert _tables(admin_engine) & set(RLS_TABLES) == set()
        assert _role(admin_engine, worker) is not None
    finally:
        command.upgrade(alembic_config, "head")
    assert set(INGESTION_TABLES) <= _tables(admin_engine)
    assert _functions(admin_engine) == FUNCTIONS
    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == HEAD


def test_worker_role_attributes(
    admin_engine: Engine, stack: ComposeProject, migrated: None
) -> None:
    assert _role(admin_engine, stack.env["POSTGRES_WORKER_USER"]) == WORKER_ATTRIBUTES
    with admin_engine.connect() as conn:
        memberships = conn.execute(
            text(
                "SELECT count(*) FROM pg_auth_members"
                " WHERE member = (SELECT oid FROM pg_roles WHERE rolname = :r)"
            ),
            {"r": stack.env["POSTGRES_WORKER_USER"]},
        ).scalar_one()
    assert memberships == 0


def test_worker_role_is_created_from_x_password_and_never_overwritten(
    alembic_config: Config,
    admin_engine: Engine,
    stack: ComposeProject,
    migrated: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing role needs a password (clear error); an existing role keeps its own."""
    monkeypatch.delenv("POSTGRES_WORKER_PASSWORD", raising=False)
    probe_cfg = alembic_cfg(stack, f"worker_role={PROBE}")  # no password at all
    command.downgrade(alembic_config, PREVIOUS)
    try:
        # No password anywhere and the role does not exist: fail clearly, change nothing.
        with pytest.raises(RuntimeError, match="no password was given"):
            command.upgrade(probe_cfg, REVISION)
        assert _role(admin_engine, PROBE) is None
        assert _tables(admin_engine) & set(INGESTION_TABLES) == set()

        # With -x worker_password the role is created; an existing role is left alone.
        first = alembic_cfg(stack, f"worker_role={PROBE}", "worker_password=first-pw")
        command.upgrade(first, REVISION)
        assert _role(admin_engine, PROBE) == WORKER_ATTRIBUTES
        command.downgrade(first, PREVIOUS)
        assert _role(admin_engine, PROBE) == WORKER_ATTRIBUTES  # the role outlives the downgrade
        second = alembic_cfg(stack, f"worker_role={PROBE}", "worker_password=second-pw")
        command.upgrade(second, REVISION)

        url = make_url(stack, "worker").set(username=PROBE, password="first-pw")
        good = create_engine(url)
        try:
            with good.connect() as conn:
                assert conn.execute(text("SELECT current_user")).scalar_one() == PROBE
        finally:
            good.dispose()
        bad = create_engine(url.set(password="second-pw"))
        try:
            with pytest.raises(OperationalError), bad.connect():
                pass
        finally:
            bad.dispose()
    finally:
        command.downgrade(alembic_config, PREVIOUS)
        with admin_engine.begin() as conn:
            conn.execute(text(f"DROP OWNED BY {PROBE}"))
            conn.execute(text(f"DROP ROLE IF EXISTS {PROBE}"))
        command.upgrade(alembic_config, "head")
    assert _role(admin_engine, PROBE) is None
    assert _functions(admin_engine) == FUNCTIONS


@pytest.mark.parametrize("bad", ["Robert;DROP TABLE x", "9lives", "a" * 70, "UPPER"])
def test_invalid_worker_role_name_is_rejected(
    alembic_config: Config, admin_engine: Engine, stack: ComposeProject, migrated: None, bad: str
) -> None:
    command.downgrade(alembic_config, PREVIOUS)
    try:
        cfg = alembic_cfg(stack, f"worker_role={bad}", "worker_password=x")
        with pytest.raises(ValueError, match="invalid worker_role name"):
            command.upgrade(cfg, REVISION)
        assert _tables(admin_engine) & set(INGESTION_TABLES) == set()
    finally:
        command.upgrade(alembic_config, "head")


def test_worker_role_must_differ_from_the_app_role(
    alembic_config: Config, admin_engine: Engine, stack: ComposeProject, migrated: None
) -> None:
    command.downgrade(alembic_config, PREVIOUS)
    try:
        cfg = alembic_cfg(
            stack, f"worker_role={stack.env['POSTGRES_APP_USER']}", "worker_password=x"
        )
        with pytest.raises(ValueError, match="must differ from the app role"):
            command.upgrade(cfg, REVISION)
    finally:
        command.upgrade(alembic_config, "head")


def test_downgrade_leaves_no_ingestion_catalog_objects(
    alembic_config: Config, admin_engine: Engine, migrated: None
) -> None:
    command.downgrade(alembic_config, PREVIOUS)
    try:
        with admin_engine.connect() as conn:
            leftovers = conn.execute(
                text(
                    "SELECT 'function:' || proname FROM pg_proc WHERE proname = ANY(:f)"
                    " UNION ALL SELECT 'trigger:' || tgname FROM pg_trigger"
                    " WHERE NOT tgisinternal AND tgname LIKE 'ingestion_job_events_%'"
                    " UNION ALL SELECT 'index:' || indexname FROM pg_indexes"
                    " WHERE indexname LIKE 'ix_ingestion_%' OR indexname LIKE 'ix_outbox_%'"
                    " OR indexname LIKE 'ix_knowledge_%'"
                    " OR indexname LIKE 'ix_document_publications_%'"
                ),
                {"f": sorted(FUNCTIONS)},
            ).all()
        assert leftovers == []
    finally:
        command.upgrade(alembic_config, "head")
