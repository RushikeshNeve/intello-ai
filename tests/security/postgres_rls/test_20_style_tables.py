"""Tenancy coverage of ``workspace_styles`` (migration 0019b, M12.07), app role.

Nothing without context, nothing from another org; a plain org member sees the org default but not
a workspace row of a workspace they do not belong to; only a workspace admin may write its row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from rls_world import Tenant, World, app_tx, scalar
from sqlalchemy import Engine, text

STYLE_TABLES: tuple[str, ...] = ("workspace_styles",)


@dataclass(frozen=True)
class StyleRows:
    tenant: Tenant
    ws_id: uuid.UUID
    default_id: uuid.UUID


def _seed(admin: Engine, t: Tenant) -> StyleRows:
    ws_id, default_id = uuid.uuid4(), uuid.uuid4()
    with admin.begin() as conn:
        for row_id, ws in ((ws_id, t.workspace_id), (default_id, None)):
            conn.execute(
                text(
                    "INSERT INTO workspace_styles (id, org_id, workspace_id, updated_by)"
                    " VALUES (:i, :o, :w, :u)"
                ),
                {"i": row_id, "o": t.org_id, "w": ws, "u": t.owner_id},
            )
    return StyleRows(t, ws_id, default_id)


@pytest.fixture(scope="module")
def styles(admin_engine: Engine, world: World) -> tuple[StyleRows, StyleRows]:
    return _seed(admin_engine, world.a), _seed(admin_engine, world.b)


@pytest.mark.parametrize("table", STYLE_TABLES)
def test_style_select_without_context_sees_nothing(
    engines: dict[str, Engine], styles: tuple[StyleRows, StyleRows], table: str
) -> None:
    with app_tx(engines["app"]) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize("table", STYLE_TABLES)
def test_style_select_is_confined_to_the_context_org(
    engines: dict[str, Engine], styles: tuple[StyleRows, StyleRows], table: str
) -> None:
    a, b = styles
    with app_tx(engines["app"], org=a.tenant.org_id, user=a.tenant.owner_id) as conn:
        assert scalar(conn, f"SELECT count(*) FROM {table}") == 2
        assert (
            scalar(conn, f"SELECT count(*) FROM {table} WHERE org_id = :o", {"o": b.tenant.org_id})
            == 0
        )


def test_workspace_member_reads_both_rows_but_cannot_write_the_workspace_row(
    engines: dict[str, Engine], styles: tuple[StyleRows, StyleRows]
) -> None:
    a = styles[0].tenant
    with app_tx(engines["app"], org=a.org_id, user=a.member_id) as conn:
        assert scalar(conn, "SELECT count(*) FROM workspace_styles") == 2
        rows = conn.execute(
            text("UPDATE workspace_styles SET tone = 'formal' WHERE workspace_id IS NOT NULL")
        ).rowcount
        assert rows == 0  # not a workspace admin: the policy filters the row out
    with app_tx(engines["app"], org=a.org_id, user=a.owner_id) as conn:
        rows = conn.execute(
            text("UPDATE workspace_styles SET tone = 'formal' WHERE workspace_id IS NOT NULL")
        ).rowcount
        assert rows == 1  # a workspace admin (org owner) may
