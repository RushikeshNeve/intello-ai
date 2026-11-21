"""Tenancy coverage of the workspace golden-set tables (migration 0017_evals, M12.03), app role.

Rows are visible only to a workspace admin (org owner here) of the row's workspace in the context
org: nothing without context, nothing from another org, nothing for a plain workspace member.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from rls_world import Tenant, World, app_tx, scalar
from sqlalchemy import Engine, text

EVAL_TABLES: tuple[str, ...] = ("eval_sets", "eval_cases", "eval_runs", "eval_run_results")
_KEY = {"eval_sets": "set", "eval_cases": "case", "eval_runs": "run", "eval_run_results": "res"}


@dataclass(frozen=True)
class EvalRows:
    tenant: Tenant
    ids: dict[str, uuid.UUID]


def _seed(admin: Engine, t: Tenant) -> EvalRows:
    ids = {k: uuid.uuid4() for k in ("set", "case", "run", "res")}
    scope = {"o": t.org_id, "w": t.workspace_id, "u": t.owner_id}
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO eval_sets (id, org_id, workspace_id, name, created_by)"
                " VALUES (:i, :o, :w, 'golden', :u)"
            ),
            {**scope, "i": ids["set"]},
        )
        conn.execute(
            text(
                "INSERT INTO eval_cases (id, org_id, workspace_id, set_id, question)"
                " VALUES (:i, :o, :w, :s, 'q?')"
            ),
            {**scope, "i": ids["case"], "s": ids["set"]},
        )
        conn.execute(
            text(
                "INSERT INTO eval_runs (id, org_id, workspace_id, set_id, set_version, created_by)"
                " VALUES (:i, :o, :w, :s, 1, :u)"
            ),
            {**scope, "i": ids["run"], "s": ids["set"]},
        )
        conn.execute(
            text(
                "INSERT INTO eval_run_results (id, org_id, workspace_id, run_id, question,"
                " answer_status) VALUES (:i, :o, :w, :r, 'q?', 'answered')"
            ),
            {**scope, "i": ids["res"], "r": ids["run"]},
        )
    return EvalRows(t, ids)


@pytest.fixture(scope="module")
def evals(admin_engine: Engine, world: World) -> tuple[EvalRows, EvalRows]:
    return _seed(admin_engine, world.a), _seed(admin_engine, world.b)


@pytest.mark.parametrize("table", EVAL_TABLES)
def test_eval_select_without_context_sees_nothing(
    engines: dict[str, Engine], evals: tuple[EvalRows, EvalRows], table: str
) -> None:
    with app_tx(engines["app"]) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize("table", EVAL_TABLES)
def test_eval_select_is_confined_to_the_context_org(
    engines: dict[str, Engine], evals: tuple[EvalRows, EvalRows], table: str
) -> None:
    a, b = evals
    with app_tx(engines["app"], org=a.tenant.org_id, user=a.tenant.owner_id) as conn:
        assert (
            scalar(conn, f"SELECT count(*) FROM {table} WHERE id = :i", {"i": a.ids[_KEY[table]]})
            == 1
        )
        assert (
            scalar(conn, f"SELECT count(*) FROM {table} WHERE id = :i", {"i": b.ids[_KEY[table]]})
            == 0
        )
        assert (
            scalar(conn, f"SELECT count(*) FROM {table} WHERE org_id <> :o", {"o": a.tenant.org_id})
            == 0
        )


@pytest.mark.parametrize("table", EVAL_TABLES)
def test_eval_rows_are_invisible_to_a_plain_workspace_member(
    engines: dict[str, Engine], evals: tuple[EvalRows, EvalRows], table: str
) -> None:
    a = evals[0].tenant
    with app_tx(engines["app"], org=a.org_id, user=a.member_id) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0
