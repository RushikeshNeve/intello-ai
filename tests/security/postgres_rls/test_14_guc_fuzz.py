"""Property-based fuzz of the ``app.current_org_id`` value against the M02 + M03 tables.

Oracle: the policies compare ``org_id = nullif(current_setting(...), '')::uuid``. So for ANY
string the outcome must be exactly that cast's: if PostgreSQL cannot cast the value to a uuid, the
query fails closed with 22P02 and returns nothing; if it can (any accepted spelling), the rows
visible are only those of that one org, and a row stamped with any other org cannot be inserted.
Bounded (``max_examples``) so the suite stays fast; hypothesis shrinks and prints a failing value.
"""

from __future__ import annotations

import uuid

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from rls_docs_world import BUILDERS, DOC_TABLES, GRANTS, ROLES, DocWorld
from rls_world import sqlstate
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

SQL_FRAGMENTS = [
    "' OR '1'='1",
    "'; DROP TABLE documents; --",
    "') OR true --",
    "\\",
    "%",
    "_",
    "{",
    "}",
    "$$",
    " ",
    "\t",
    "\n",
    "--",
    "/*",
    "NULL",
    "00000000-0000-0000-0000-000000000000",
    "‮",
    "é",
    "\U0001f4a5",
]

# NUL cannot be sent to PostgreSQL; lone surrogates cannot be encoded as UTF-8.
SAFE_TEXT = st.text(
    alphabet=st.characters(codec="utf-8", exclude_characters=chr(0)),
    max_size=80,
)


def _spellings(org: uuid.UUID) -> st.SearchStrategy[str]:
    """Valid and near-valid spellings of a real org id (the interesting neighbourhood)."""
    s = str(org)
    base = st.sampled_from([s, s.upper(), s.replace("-", ""), "{" + s + "}", " " + s, s + " "])
    return st.builds(
        lambda core, pre, post: pre + core + post,
        base,
        st.one_of(st.just(""), st.sampled_from(SQL_FRAGMENTS), SAFE_TEXT.map(lambda t: t[:6])),
        st.one_of(st.just(""), st.sampled_from(SQL_FRAGMENTS), SAFE_TEXT.map(lambda t: t[:6])),
    )


def _guc_values(docs: DocWorld) -> st.SearchStrategy[str]:
    return st.one_of(
        SAFE_TEXT,
        st.sampled_from(SQL_FRAGMENTS),
        st.lists(st.sampled_from(SQL_FRAGMENTS), min_size=1, max_size=4).map("".join),
        st.text(alphabet="0123456789abcdefABCDEF-{} ", max_size=45),
        _spellings(docs.a.org),
        _spellings(docs.b.org),
        st.uuids().map(str),
    )


def _cast(engine: Engine, value: str) -> uuid.UUID | str | None:
    """What PostgreSQL makes of the value: a UUID, None for empty, or 'error' (22P02)."""
    try:
        with engine.connect() as conn:
            got = conn.execute(text("SELECT CAST(nullif(:v, '') AS uuid)"), {"v": value}).scalar()
            conn.rollback()
    except DBAPIError as exc:
        assert sqlstate(exc) == "22P02"
        return "error"
    return got


@pytest.mark.parametrize("role", ROLES)
def test_fuzz_org_guc_reads_follow_the_uuid_cast_exactly(
    engines: dict[str, Engine], docs: DocWorld, role: str
) -> None:
    engine = engines[role]
    readable = [t for t in DOC_TABLES if GRANTS[role][t].select]

    @settings(
        max_examples=120,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
        database=None,
    )
    @given(value=_guc_values(docs), table=st.sampled_from(readable))
    def check(value: str, table: str) -> None:
        expected = _cast(engine, value)
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(
                    text("SELECT set_config('app.current_org_id', :v, true)"), {"v": value}
                )
                try:
                    seen = set(conn.execute(text(f"SELECT DISTINCT org_id FROM {table}")).scalars())
                except DBAPIError as exc:
                    assert expected == "error", (value, expected, exc.orig)
                    assert sqlstate(exc) == "22P02"
                    return
                assert expected != "error", (value, seen)
                if expected is None:
                    assert seen == set(), (value, seen)
                else:
                    assert seen <= {expected}, (value, expected, seen)
                    assert seen <= {docs.a.org, docs.b.org}
            finally:
                trans.rollback()

    check()


@pytest.mark.parametrize("role", ROLES)
def test_fuzz_org_guc_writes_cannot_stamp_a_foreign_org(
    engines: dict[str, Engine], docs: DocWorld, role: str
) -> None:
    """With a fuzzed context, an INSERT stamped with org B (A's parents or B's) succeeds only when
    the context itself casts to exactly B (the documented trust boundary); never otherwise."""
    engine = engines[role]
    insertable = [t for t in DOC_TABLES if GRANTS[role][t].insert]

    @settings(
        max_examples=120,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
        database=None,
    )
    @given(value=_guc_values(docs), table=st.sampled_from(insertable), foreign=st.booleans())
    def check(value: str, table: str, foreign: bool) -> None:
        expected = _cast(engine, value)
        target = docs.b if foreign else docs.a
        sql, params = BUILDERS[table](target.org, target.owner, target.ids())
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(
                    text("SELECT set_config('app.current_org_id', :v, true)"), {"v": value}
                )
                try:
                    conn.execute(text(sql), params)
                except DBAPIError as exc:
                    assert sqlstate(exc) in ("22P02", "42501", "23503", "23505"), exc.orig
                    return
                # succeeded: only legitimate if the context IS the row's org
                assert expected == target.org, (value, expected, table)
            finally:
                trans.rollback()

    check()


# ruff: noqa: PT017
