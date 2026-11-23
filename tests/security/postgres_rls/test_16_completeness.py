"""Completeness gate: a table that gains RLS without tenancy tests fails this suite.

Three layers:

1. the explicit registries (``ORG_TABLE_SPECS`` for the M01 tables, ``DOC_TABLES`` plus its
   grant / builder / key registries for M02 + M03) must together equal ``ORG_SCOPED_TABLES``;
2. the live database must have RLS (enabled and forced) on exactly the tables the metadata
   declares, and no other ordinary table in ``public``;
3. when the whole suite is collected, every RLS table must appear as the parameter of at least
   one no-context test and one cross-org test.
"""

from __future__ import annotations

import pytest
from rls_chat_world import CHAT_GRANTS, CHAT_TABLES
from rls_chat_world import ROLES as CHAT_ROLES
from rls_connector_world import CONNECTOR_TABLES
from rls_docs_world import BUILDERS, DOC_TABLES, GRANTS, PARENTS, PK, ROLES
from rls_m07_world import M07_TABLES
from rls_world import ORG_TABLE_SPECS
from sqlalchemy import Engine, text
from test_18_graph_tables import GRAPH_TABLES
from test_20_embedding_cache import CACHE_TABLES
from test_20_eval_tables import EVAL_TABLES
from test_20_prompt_versions import PROMPT_TABLES
from test_20_reindex_tables import REINDEX_TABLES
from test_20_style_tables import STYLE_TABLES
from test_20_tenant_keys import TENANT_KEY_TABLES
from test_20_usage_tables import USAGE_TABLES
from test_20_wave_b_tables import WAVE_B_TABLES

from app.core.models import ORG_SCOPED_TABLES, RLS_TABLES, Base

NO_CONTEXT_TESTS = {
    "test_select_unset_context_sees_nothing",  # test_02 (M01 tables + users)
    "test_select_without_context_sees_nothing",  # test_11 (M02 + M03, both roles)
    "test_chat_select_without_context_sees_nothing",  # test_17 (M04, both roles)
    "test_connector_select_without_context_sees_nothing",  # test_18 (M06, both roles)
    "test_graph_select_without_context_sees_nothing",  # test_18 (M05, both roles)
    "test_m07_select_without_context_sees_nothing",  # test_19 (M07, both roles)
    "test_usage_select_without_context_sees_nothing",  # test_20 (M11.01, both roles)
    "test_prompt_select_without_context_sees_nothing",  # test_20 (M12.01, both roles)
    "test_eval_select_without_context_sees_nothing",  # test_20 (M12.03)
    "test_style_select_without_context_sees_nothing",  # test_20 (M12.07)
    "test_cache_select_without_context_sees_nothing",  # test_20 (M11.03, both roles)
    "test_reindex_select_without_context_sees_nothing",  # test_20 (M12.05, both roles)
    "test_waveb_select_without_context_sees_nothing",  # test_20 (M11.08/M12.06/M12.09)
    "test_tenant_keys_select_without_context_sees_nothing",  # test_20 (M14.08, both roles)
}
CROSS_ORG_TESTS = {
    "test_only_own_org_rows_visible",  # test_03 (M01 org-scoped tables)
    "test_select_is_confined_to_the_context_org",  # test_11
    "test_chat_select_is_confined_to_the_context_org",  # test_17
    "test_connector_select_is_confined_to_the_context_org",  # test_18 (M06)
    "test_graph_select_is_confined_to_the_context_org",  # test_18 (M05)
    "test_m07_select_is_confined_to_the_context_org",  # test_19 (M07)
    "test_usage_select_is_confined_to_the_context_org",  # test_20 (M11.01)
    "test_prompt_select_is_confined_to_the_context_org",  # test_20 (M12.01)
    "test_eval_select_is_confined_to_the_context_org",  # test_20 (M12.03)
    "test_style_select_is_confined_to_the_context_org",  # test_20 (M12.07)
    "test_cache_select_is_confined_to_the_context_org",  # test_20 (M11.03)
    "test_reindex_select_is_confined_to_the_context_org",  # test_20 (M12.05)
    "test_waveb_select_is_confined_to_the_context_org",  # test_20 (M11.08/M12.06/M12.09)
    "test_tenant_keys_select_is_confined_to_the_context_org",  # test_20 (M14.08)
}


def test_registries_cover_every_org_scoped_table_exactly_once() -> None:
    assert set(DOC_TABLES).isdisjoint(ORG_TABLE_SPECS)
    assert set(CHAT_TABLES).isdisjoint(ORG_TABLE_SPECS)
    assert set(CHAT_TABLES).isdisjoint(DOC_TABLES)
    assert set(CONNECTOR_TABLES).isdisjoint(set(DOC_TABLES) | set(CHAT_TABLES))
    assert set(GRAPH_TABLES).isdisjoint(set(ORG_TABLE_SPECS) | set(DOC_TABLES) | set(CHAT_TABLES))
    declared = (
        set(ORG_TABLE_SPECS)
        | set(DOC_TABLES)
        | set(CHAT_TABLES)
        | set(GRAPH_TABLES)
        | set(CONNECTOR_TABLES)
        | set(M07_TABLES)
        | set(USAGE_TABLES)
        | set(PROMPT_TABLES)
        | set(EVAL_TABLES)
        | set(STYLE_TABLES)
        | set(CACHE_TABLES)
        | set(REINDEX_TABLES)
        | set(WAVE_B_TABLES)
        | set(TENANT_KEY_TABLES)
    )
    assert declared == set(ORG_SCOPED_TABLES), (
        "a table was added to ORG_SCOPED_TABLES without a tenancy registry entry: add it to "
        "rls_world.ORG_TABLE_SPECS (M01), rls_docs_world.DOC_TABLES + GRANTS/BUILDERS/PK/PARENTS "
        "(M02/M03), rls_chat_world.CHAT_TABLES + CHAT_GRANTS (M04), "
        "test_18_graph_tables.GRAPH_TABLES (M05), rls_connector_world.CONNECTOR_TABLES (M06) or "
        "rls_m07_world.M07_TABLES (M07)"
    )
    assert len(DOC_TABLES) == len(set(DOC_TABLES))
    assert len(CHAT_TABLES) == len(set(CHAT_TABLES))


def test_doc_registries_are_complete_for_both_roles() -> None:
    assert set(BUILDERS) == set(PK) == set(PARENTS) == set(DOC_TABLES)
    for role in ROLES:
        assert set(GRANTS[role]) == set(DOC_TABLES), role


def test_chat_registry_is_complete_for_both_roles() -> None:
    for role in CHAT_ROLES:
        assert set(CHAT_GRANTS[role]) == set(CHAT_TABLES), role


def test_metadata_matches_the_declared_rls_tables() -> None:
    assert set(Base.metadata.tables) == set(RLS_TABLES)


def test_database_has_rls_on_exactly_the_declared_tables(
    admin_engine: Engine, migrated: None
) -> None:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
            )
        ).all()
    tables = {name: (rls, force) for name, rls, force in rows}
    tables.pop("alembic_version", None)
    assert set(tables) == set(RLS_TABLES), (
        "ordinary table(s) in public that are not in RLS_TABLES: "
        f"{sorted(set(tables) - set(RLS_TABLES))}; missing: {sorted(set(RLS_TABLES) - set(tables))}"
    )
    assert all(v == (True, True) for v in tables.values()), tables


def test_every_rls_table_has_tenancy_tests_in_this_suite(request: pytest.FixtureRequest) -> None:
    items = request.session.items
    names = {i.originalname or i.name for i in items if hasattr(i, "originalname")}
    if not (NO_CONTEXT_TESTS & names and CROSS_ORG_TESTS & names):
        pytest.skip("not the whole suite: collection check needs test_02/03/11 collected")

    def tables_of(test_names: set[str]) -> set[str]:
        found: set[str] = set()
        for item in items:
            if (getattr(item, "originalname", None) or "") not in test_names:
                continue
            params = getattr(getattr(item, "callspec", None), "params", {})
            if "table" in params:
                found.add(params["table"])
        return found

    no_context = tables_of(NO_CONTEXT_TESTS)
    cross_org = tables_of(CROSS_ORG_TESTS)
    assert set(RLS_TABLES) <= no_context, f"no no-context test for {set(RLS_TABLES) - no_context}"
    # `users` is not org-keyed; its isolation tests are test_06 (co-member visibility).
    assert set(ORG_SCOPED_TABLES) <= cross_org, (
        f"no cross-org test for {set(ORG_SCOPED_TABLES) - cross_org}"
    )
