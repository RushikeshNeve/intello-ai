"""The six M03 SECURITY DEFINER functions: the worker's only cross-org window.

Documented behaviour (m03-contracts.md §4): executable by the worker role alone; they ignore the
RLS context by design (they run as the owner, who bypasses RLS) and return ids / small scalars
only. These tests pin that surface and attack it: argument injection, ``search_path`` hijack via
the one schema a caller controls (``pg_temp``), replace/alter attempts, and abuse of the context.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from rls_docs_world import DocWorld, admin_tx, become
from rls_world import app_tx, expect_denied, scalar, set_ctx
from sqlalchemy import Connection, Engine, text

# (name, full signature, example call with bind parameters)
FUNCTIONS: dict[str, tuple[str, str]] = {
    "ingest_claim_job": ("ingest_claim_job(uuid, text, integer)", "ingest_claim_job(:j, 'w', 60)"),
    "ingest_reap_expired_leases": (
        "ingest_reap_expired_leases(integer)",
        "ingest_reap_expired_leases(1)",
    ),
    "outbox_fetch_batch": ("outbox_fetch_batch(integer)", "outbox_fetch_batch(10)"),
    "outbox_mark_dispatched": (
        "outbox_mark_dispatched(uuid[])",
        "outbox_mark_dispatched(ARRAY[:o]::uuid[])",
    ),
    "outbox_mark_failed": (
        "outbox_mark_failed(uuid, text, integer)",
        "outbox_mark_failed(:o, 'e', 1)",
    ),
    "knowledge_next_reconcile_batch": (
        "knowledge_next_reconcile_batch(integer, integer)",
        "knowledge_next_reconcile_batch(10, 0)",
    ),
}
NAMES = sorted(FUNCTIONS)

CLAIM = "SELECT * FROM public.ingest_claim_job(:j, :o, :l)"


def _rows(conn: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(text(sql), params).mappings()]


# -- EXECUTE privilege ----------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_execute_is_granted_to_the_worker_alone(
    admin_engine: Engine, migrated: None, app_role: str, worker_role: str, name: str
) -> None:
    signature = FUNCTIONS[name][0]
    with admin_engine.connect() as conn:
        acl, secdef = conn.execute(
            text(
                "SELECT proacl::text, prosecdef FROM pg_proc WHERE oid = CAST(:s AS regprocedure)"
            ),
            {"s": f"public.{signature}"},
        ).one()
        can = {
            who: conn.execute(
                text("SELECT has_function_privilege(:r, CAST(:s AS regprocedure), 'EXECUTE')"),
                {"r": who, "s": f"public.{signature}"},
            ).scalar_one()
            for who in (app_role, worker_role)
        }
    assert secdef is True
    assert can == {app_role: False, worker_role: True}
    grantees = {i.split("=")[0] for i in acl.strip("{}").split(",")}
    assert "" not in grantees, f"PUBLIC may EXECUTE {name}: {acl}"
    assert worker_role in grantees and app_role not in grantees


def test_definer_search_path_is_pinned_with_pg_temp_last(
    admin_engine: Engine, migrated: None
) -> None:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT p.proname, p.proconfig FROM pg_proc p JOIN pg_namespace n"
                " ON n.oid = p.pronamespace WHERE p.prosecdef AND n.nspname = 'public'"
            )
        ).all()
    assert {r.proname for r in rows} >= set(NAMES)
    for name, config in rows:
        assert config == ["search_path=pg_catalog, public, pg_temp"], name


@pytest.mark.parametrize("name", NAMES)
def test_app_role_cannot_call_any_m03_definer(
    app_engine: Engine, docs: DocWorld, name: str
) -> None:
    call = FUNCTIONS[name][1]
    expect_denied(
        app_engine,
        f"SELECT * FROM {call}",
        {"j": docs.a.job, "o": docs.a.outbox},
        match="permission denied",
        org=docs.a.org,
    )


@pytest.mark.parametrize(
    "fn",
    [
        "ukos_document_versions_immutable",
        "ukos_upload_sessions_forward_only",
        "ukos_ingestion_job_events_append_only",
    ],
)
@pytest.mark.parametrize("role", ["app", "worker"])
def test_trigger_functions_are_not_callable_by_roles(
    engines: dict[str, Engine], role: str, fn: str
) -> None:
    expect_denied(engines[role], f"SELECT public.{fn}()", match="permission denied")


@pytest.mark.parametrize("name", NAMES)
def test_roles_cannot_replace_alter_or_drop_a_definer(
    worker_engine: Engine, app_engine: Engine, name: str
) -> None:
    signature = FUNCTIONS[name][0]
    for engine in (worker_engine, app_engine):
        for ddl in (
            f"ALTER FUNCTION public.{signature} SET search_path = pg_temp",
            f"ALTER FUNCTION public.{signature} RESET ALL",
            f"ALTER FUNCTION public.{signature} SECURITY INVOKER",
            f"ALTER FUNCTION public.{signature} OWNER TO CURRENT_USER",
            f"DROP FUNCTION public.{signature}",
        ):
            expect_denied(engine, ddl, states=("42501",))


def test_roles_cannot_create_objects_in_any_shared_schema(
    worker_engine: Engine, app_engine: Engine
) -> None:
    for engine in (worker_engine, app_engine):
        for ddl in (
            "CREATE SCHEMA evil",
            "CREATE TABLE public.evil (x int)",
            "CREATE FUNCTION public.now() RETURNS timestamptz LANGUAGE sql AS 'SELECT now()'",
            "CREATE FUNCTION pg_catalog.evil() RETURNS int LANGUAGE sql AS 'SELECT 1'",
            "CREATE OR REPLACE FUNCTION public.outbox_fetch_batch(integer) RETURNS int"
            " LANGUAGE sql AS 'SELECT 1'",
        ):
            expect_denied(engine, ddl, states=("42501", "42883"))


# -- claim ----------------------------------------------------------------------------------


def test_claim_returns_only_ids_and_counters_and_crosses_orgs_by_design(
    worker_engine: Engine, docs: DocWorld
) -> None:
    """DOCUMENTED: claim is targeted by job id and works whatever org context the worker holds
    (none, another org, garbage). It returns ids and two counters, never titles or content."""
    b = docs.b
    for ctx in (None, docs.a.org):
        with app_tx(worker_engine, org=ctx) as conn:
            got = _rows(conn, CLAIM, j=b.job, o="w-1", l=60)
            assert len(got) == 1
            row = got[0]
            assert set(row) == {
                "job_id", "org_id", "workspace_id", "document_id", "version_id",
                "revision_id", "attempts", "lease_epoch",
            }  # fmt: skip
            assert row["org_id"] == b.org and row["job_id"] == b.job
            assert (row["attempts"], row["lease_epoch"]) == (1, 1)
            # the claim took effect, visible only under the returned org
            set_ctx(conn, org=b.org)
            assert (
                scalar(conn, "SELECT state FROM ingestion_jobs WHERE id = :j", {"j": b.job})
                == "running"
            )
            assert (
                scalar(
                    conn,
                    "SELECT count(*) FROM ingestion_job_events WHERE job_id = :j AND event_type = 'started'",
                    {"j": b.job},
                )
                == 1
            )
            set_ctx(conn, org=docs.a.org)
            assert (
                scalar(conn, "SELECT count(*) FROM ingestion_jobs WHERE id = :j", {"j": b.job}) == 0
            )


def test_claim_rolled_back_leaves_the_job_queued(
    worker_engine: Engine, admin_engine: Engine, docs: DocWorld
) -> None:
    with app_tx(worker_engine) as conn:
        assert len(_rows(conn, CLAIM, j=docs.a.job, o="w", l=30)) == 1
    with admin_engine.connect() as conn:
        assert (
            scalar(conn, "SELECT state FROM ingestion_jobs WHERE id = :j", {"j": docs.a.job})
            == "queued"
        )


@pytest.mark.parametrize("which", ["unknown", "running", "other_org_running"])
def test_claim_of_a_job_that_cannot_be_claimed_is_an_empty_result_without_oracle(
    worker_engine: Engine, docs: DocWorld, which: str
) -> None:
    job = {
        "unknown": uuid.uuid4(),
        "running": docs.a.running_job,
        "other_org_running": docs.b.running_job,
    }[which]
    with app_tx(worker_engine, org=docs.a.org) as conn:
        assert _rows(conn, CLAIM, j=job, o="w", l=30) == []


HOSTILE = [
    "x'); DROP TABLE ingestion_jobs; --",
    "'; UPDATE ingestion_jobs SET state = 'succeeded'; --",
    '" OR 1=1 --',
    "$$; SELECT pg_sleep(30); $$",
    "\\'; --",
    "é‮\U0001f4a5" * 20,
    "o" * 10_000,
    "%s %(x)s {0} {x}",
]


@pytest.mark.parametrize("owner", HOSTILE, ids=[f"h{i}" for i in range(len(HOSTILE))])
def test_claim_owner_argument_is_data_not_sql(
    worker_engine: Engine, admin_engine: Engine, docs: DocWorld, owner: str
) -> None:
    with app_tx(worker_engine, org=docs.a.org) as conn:
        assert len(_rows(conn, CLAIM, j=docs.a.job, o=owner, l=30)) == 1
        assert (
            scalar(conn, "SELECT lease_owner FROM ingestion_jobs WHERE id = :j", {"j": docs.a.job})
            == owner
        )
        # nothing else changed: the other job of the org is still queued, tables still exist
        assert (
            scalar(
                conn, "SELECT state FROM ingestion_jobs WHERE id = :j", {"j": docs.a.running_job}
            )
            == "running"
        )
        assert scalar(conn, "SELECT count(*) FROM ingestion_jobs") == 2
    with admin_engine.connect() as conn:
        assert scalar(conn, "SELECT to_regclass('public.ingestion_jobs') IS NOT NULL") is True
        assert (
            scalar(
                conn,
                "SELECT count(*) FROM ingestion_jobs WHERE org_id = :o AND state = 'succeeded'",
                {"o": docs.a.org},
            )
            == 0
        )


@pytest.mark.parametrize(
    ("owner", "lease"),
    [("", 60), (None, 60), ("w", 0), ("w", -5), ("w", None)],
)
def test_claim_rejects_invalid_arguments(
    worker_engine: Engine, docs: DocWorld, owner: str | None, lease: int | None
) -> None:
    expect_denied(
        worker_engine,
        CLAIM,
        {"j": docs.a.job, "o": owner, "l": lease},
        states=("22023",),
        org=docs.a.org,
    )


@pytest.mark.parametrize("bad", ["' OR true --", "*", "not-a-uuid", "‮"])
def test_garbage_context_does_not_affect_definers(
    worker_engine: Engine, docs: DocWorld, bad: str
) -> None:
    """The definers never read the org GUC: a malformed value neither errors nor changes them."""
    with worker_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("SELECT set_config('app.current_org_id', :v, true)"), {"v": bad})
            assert len(_rows(conn, CLAIM, j=docs.a.job, o="w", l=30)) == 1
            assert len(_rows(conn, "SELECT * FROM outbox_fetch_batch(100)")) >= 2
        finally:
            trans.rollback()


def test_claim_of_a_job_of_a_soft_deleted_document_is_not_blocked_here_documented(
    admin_engine: Engine, role_names: dict[str, str], docs: DocWorld
) -> None:
    """DOCUMENTED: the claim does not look at ``documents.deleted_at``; the runner's per-stage
    check and the fenced publish (m03 §6) cancel work for deleted documents. The definer still
    returns ids of the job's OWN org only."""
    b = docs.b
    with admin_tx(admin_engine) as conn:
        conn.execute(
            text("UPDATE documents SET deleted_at = now(), deleted_by = :u WHERE id = :d"),
            {"u": b.owner, "d": b.doc2},
        )
        become(conn, role_names["worker"], org=docs.a.org)
        got = _rows(conn, CLAIM, j=b.job, o="w", l=30)
        assert [g["org_id"] for g in got] == [b.org]


# -- reap -----------------------------------------------------------------------------------


def test_reap_requeues_expired_leases_of_every_org_and_emits_one_outbox_row_each(
    worker_engine: Engine, docs: DocWorld
) -> None:
    """DOCUMENTED cross-org: expired leases of all orgs are handled in one call."""
    with app_tx(worker_engine, org=docs.a.org) as conn:
        assert scalar(conn, "SELECT ingest_reap_expired_leases(1)") >= 2
        for scope in (docs.a, docs.b):
            set_ctx(conn, org=scope.org)
            state, owner = conn.execute(
                text("SELECT state, lease_owner FROM ingestion_jobs WHERE id = :j"),
                {"j": scope.running_job},
            ).one()
            assert (state, owner) == ("queued", None)
            assert (
                scalar(
                    conn,
                    "SELECT count(*) FROM ingestion_job_events WHERE job_id = :j"
                    " AND event_type = 'retry_scheduled'",
                    {"j": scope.running_job},
                )
                == 1
            )
            assert (
                scalar(
                    conn,
                    "SELECT count(*) FROM outbox_events WHERE aggregate_id = :j"
                    " AND org_id = :o AND event_type = 'ingestion.job.queued'",
                    {"j": scope.running_job, "o": scope.org},
                )
                == 1
            )
        # events were written under the job's own org, never the caller's
        set_ctx(conn, org=docs.a.org)
        assert (
            scalar(
                conn,
                "SELECT count(*) FROM ingestion_job_events WHERE job_id = :j",
                {"j": docs.b.running_job},
            )
            == 0
        )


def test_reap_marks_exhausted_jobs_dead(
    admin_engine: Engine, role_names: dict[str, str], docs: DocWorld
) -> None:
    a = docs.a
    with admin_tx(admin_engine) as conn:
        conn.execute(
            text("UPDATE ingestion_jobs SET attempts = max_attempts WHERE id = :j"),
            {"j": a.running_job},
        )
        become(conn, role_names["worker"])
        scalar(conn, "SELECT ingest_reap_expired_leases(1)")
        set_ctx(conn, org=a.org)
        assert conn.execute(
            text(
                "SELECT state, last_error_code, finished_at IS NOT NULL FROM ingestion_jobs WHERE id = :j"
            ),
            {"j": a.running_job},
        ).one() == ("dead", "lease_expired", True)


@pytest.mark.parametrize("arg", ["-1", "NULL"])
def test_reap_rejects_invalid_backoff(worker_engine: Engine, arg: str) -> None:
    expect_denied(worker_engine, f"SELECT ingest_reap_expired_leases({arg})", states=("22023",))


# -- outbox ---------------------------------------------------------------------------------


def test_outbox_fetch_returns_rows_of_all_orgs_with_the_documented_columns(
    worker_engine: Engine, docs: DocWorld
) -> None:
    with app_tx(worker_engine) as conn:  # no context at all
        got = _rows(conn, "SELECT * FROM outbox_fetch_batch(1000)")
    assert set(got[0]) == {
        "id", "seq", "org_id", "aggregate_type", "aggregate_id", "event_type", "payload", "attempts",
    }  # fmt: skip
    assert {docs.a.org, docs.b.org} <= {r["org_id"] for r in got}
    assert [r["seq"] for r in got] == sorted(r["seq"] for r in got)


def test_outbox_fetch_limit_and_invalid_limit(worker_engine: Engine, docs: DocWorld) -> None:
    with app_tx(worker_engine) as conn:
        assert len(_rows(conn, "SELECT * FROM outbox_fetch_batch(1)")) == 1
    for bad in ("0", "-3", "NULL"):
        expect_denied(worker_engine, f"SELECT * FROM outbox_fetch_batch({bad})", states=("22023",))


def test_outbox_fetch_locks_rows_so_two_dispatchers_never_share_one(
    worker_engine: Engine, docs: DocWorld
) -> None:
    c1, c2 = worker_engine.connect(), worker_engine.connect()
    try:
        t1, t2 = c1.begin(), c2.begin()
        first = {r["id"] for r in _rows(c1, "SELECT * FROM outbox_fetch_batch(1000)")}
        second = {r["id"] for r in _rows(c2, "SELECT * FROM outbox_fetch_batch(1000)")}
        assert {docs.a.outbox, docs.b.outbox} <= first
        assert first.isdisjoint(second)
        t1.rollback()
        t2.rollback()
    finally:
        c1.close()
        c2.close()


def test_mark_dispatched_crosses_orgs_by_design_and_ignores_garbage(
    worker_engine: Engine, docs: DocWorld
) -> None:
    """DOCUMENTED: by id, whatever the org context (the dispatcher is org-less)."""
    with app_tx(worker_engine, org=docs.a.org) as conn:
        assert (
            scalar(conn, "SELECT outbox_mark_dispatched(ARRAY[:i]::uuid[])", {"i": docs.b.outbox})
            == 1
        )
        assert (
            scalar(conn, "SELECT outbox_mark_dispatched(ARRAY[:i]::uuid[])", {"i": docs.b.outbox})
            == 0
        )
        assert scalar(conn, "SELECT outbox_mark_dispatched(ARRAY[]::uuid[])") == 0
        assert scalar(conn, "SELECT outbox_mark_dispatched(NULL)") == 0
        assert scalar(conn, "SELECT outbox_mark_dispatched(ARRAY[gen_random_uuid()])") == 0
        set_ctx(conn, org=docs.b.org)
        assert (
            scalar(
                conn,
                "SELECT dispatched_at IS NOT NULL FROM outbox_events WHERE id = :i",
                {"i": docs.b.outbox},
            )
            is True
        )
        set_ctx(conn, org=docs.a.org)  # the caller's own org's event was NOT touched
        assert (
            scalar(
                conn,
                "SELECT dispatched_at IS NULL FROM outbox_events WHERE id = :i",
                {"i": docs.a.outbox},
            )
            is True
        )


@pytest.mark.parametrize("error", HOSTILE, ids=[f"h{i}" for i in range(len(HOSTILE))])
def test_mark_failed_error_text_is_data_and_is_truncated(
    worker_engine: Engine, docs: DocWorld, error: str
) -> None:
    with app_tx(worker_engine, org=docs.b.org) as conn:
        assert (
            scalar(conn, "SELECT outbox_mark_failed(:i, :e, 5)", {"i": docs.b.outbox, "e": error})
            is True
        )
        stored, attempts = conn.execute(
            text("SELECT last_error, attempts FROM outbox_events WHERE id = :i"),
            {"i": docs.b.outbox},
        ).one()
        assert stored == error[:500]
        assert attempts == 1
        assert scalar(conn, "SELECT to_regclass('public.outbox_events') IS NOT NULL") is True
        assert (
            scalar(conn, "SELECT count(*) FROM outbox_events") == 1
        )  # org B still has its one row


def test_mark_failed_unknown_or_dispatched_event_and_invalid_delay(
    worker_engine: Engine, docs: DocWorld
) -> None:
    with app_tx(worker_engine) as conn:
        assert scalar(conn, "SELECT outbox_mark_failed(gen_random_uuid(), 'e', 1)") is False
        scalar(conn, "SELECT outbox_mark_dispatched(ARRAY[:i]::uuid[])", {"i": docs.a.outbox})
        assert scalar(conn, "SELECT outbox_mark_failed(:i, 'e', 1)", {"i": docs.a.outbox}) is False
    for delay in ("-1", "NULL"):
        expect_denied(
            worker_engine,
            f"SELECT outbox_mark_failed(:i, 'e', {delay})",
            {"i": docs.a.outbox},
            states=("22023",),
        )


# -- reconcile ------------------------------------------------------------------------------


def test_reconcile_batch_is_cross_org_returns_ids_only_and_marks_reconciled(
    worker_engine: Engine, docs: DocWorld
) -> None:
    with app_tx(worker_engine) as conn:
        got = _rows(conn, "SELECT * FROM knowledge_next_reconcile_batch(100, 0)")
        assert set(got[0]) == {"org_id", "document_id", "revision_id"}
        mine = {(r["org_id"], r["document_id"], r["revision_id"]) for r in got}
        assert (docs.a.org, docs.a.doc, docs.a.revision) in mine
        assert (docs.b.org, docs.b.doc, docs.b.revision) in mine
        # marked: not due again for an hour
        again = _rows(conn, "SELECT * FROM knowledge_next_reconcile_batch(100, 3600)")
        assert not {r["document_id"] for r in again} & {docs.a.doc, docs.b.doc}


@pytest.mark.parametrize("args", ["0, 0", "-1, 0", "1, -1", "NULL, 0", "1, NULL"])
def test_reconcile_rejects_invalid_arguments(worker_engine: Engine, args: str) -> None:
    expect_denied(
        worker_engine, f"SELECT * FROM knowledge_next_reconcile_batch({args})", states=("22023",)
    )


# -- search_path hijack via pg_temp --------------------------------------------------------------

SHADOWS = [
    "CREATE TEMP TABLE ingestion_jobs (LIKE public.ingestion_jobs INCLUDING DEFAULTS)",
    "CREATE TEMP TABLE ingestion_job_events (LIKE public.ingestion_job_events INCLUDING DEFAULTS)",
    "CREATE TEMP TABLE outbox_events (LIKE public.outbox_events INCLUDING DEFAULTS)",
    "CREATE TEMP TABLE document_publications (LIKE public.document_publications INCLUDING DEFAULTS)",
    "CREATE FUNCTION pg_temp.now() RETURNS timestamptz LANGUAGE sql AS $$ SELECT 'infinity'::timestamptz $$",
    "CREATE FUNCTION pg_temp.make_interval(secs double precision) RETURNS interval LANGUAGE sql AS"
    " $$ SELECT '100 years'::interval $$",
    "CREATE FUNCTION pg_temp.length(text) RETURNS int LANGUAGE sql AS $$ SELECT 0 $$",
    "CREATE FUNCTION pg_temp.left(text, int) RETURNS text LANGUAGE sql AS $$ SELECT 'pwned' $$",
    "CREATE FUNCTION pg_temp.least(a double precision, b double precision) RETURNS double precision"
    " LANGUAGE sql AS $$ SELECT 0::double precision $$",
]


def _hijack(conn: Connection) -> None:
    for ddl in SHADOWS:
        conn.execute(text(ddl))
    # Put the shadow schema FIRST, the strongest position a caller can force for itself.
    conn.execute(text("SET LOCAL search_path = pg_temp, public, pg_catalog"))


def test_shadow_objects_in_pg_temp_cannot_hijack_any_definer(
    worker_engine: Engine, docs: DocWorld
) -> None:
    a, b = docs.a, docs.b
    with app_tx(worker_engine, org=a.org) as conn:
        _hijack(conn)
        # claim: real row is claimed with the real clock (lease in the near future, not infinity)
        got = _rows(conn, CLAIM, j=a.job, o="w", l=30)
        assert len(got) == 1 and got[0]["attempts"] == 1
        # reap: the real expired job is handled (and with the real now())
        assert scalar(conn, "SELECT ingest_reap_expired_leases(1)") >= 2
        # outbox
        fetched = _rows(conn, "SELECT * FROM outbox_fetch_batch(1000)")
        assert {a.outbox, b.outbox} <= {r["id"] for r in fetched}
        assert (
            scalar(conn, "SELECT outbox_mark_dispatched(ARRAY[:i]::uuid[])", {"i": a.outbox}) == 1
        )
        assert (
            scalar(conn, "SELECT outbox_mark_failed(:i, :e, 5)", {"i": b.outbox, "e": "x" * 600})
            is True
        )
        # reconcile
        rec = _rows(conn, "SELECT * FROM knowledge_next_reconcile_batch(100, 0)")
        assert {a.doc, b.doc} <= {r["document_id"] for r in rec}
        # the shadow tables stayed empty: nothing was redirected into them
        for shadow in (
            "ingestion_jobs",
            "ingestion_job_events",
            "outbox_events",
            "document_publications",
        ):
            assert scalar(conn, f"SELECT count(*) FROM pg_temp.{shadow}") == 0, shadow
        # and the real effects are in the real tables
        conn.execute(text("SET LOCAL search_path = public, pg_catalog"))
        set_ctx(conn, org=b.org)
        assert (
            scalar(
                conn,
                "SELECT length(last_error) FROM public.outbox_events WHERE id = :i",
                {"i": b.outbox},
            )
            == 500
        )


def test_claim_lease_uses_the_real_clock_despite_a_shadow_now(
    worker_engine: Engine, docs: DocWorld
) -> None:
    with app_tx(worker_engine, org=docs.a.org) as conn:
        _hijack(conn)
        assert len(_rows(conn, CLAIM, j=docs.a.job, o="w", l=30)) == 1
        conn.execute(text("SET LOCAL search_path = public, pg_catalog"))
        near = scalar(
            conn,
            "SELECT lease_expires_at BETWEEN now() AND now() + interval '5 minutes'"
            " FROM public.ingestion_jobs WHERE id = :j",
            {"j": docs.a.job},
        )
        assert near is True


def test_session_search_path_changes_do_not_leak_into_definer_behaviour(
    worker_engine: Engine, docs: DocWorld
) -> None:
    """An empty / hostile session search_path (even naming a missing schema) still resolves the
    pinned one inside the function."""
    for path in ("''", "evil, pg_temp", "pg_temp", '"$user", public'):
        with app_tx(worker_engine, org=docs.a.org) as conn:
            conn.execute(text(f"SET LOCAL search_path = {path}"))
            assert len(_rows(conn, CLAIM, j=docs.a.job, o="w", l=30)) == 1, path


# ruff: noqa: E501, PT018
