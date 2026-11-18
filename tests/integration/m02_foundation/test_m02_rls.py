"""RLS smoke for the M02 tables as the app role ``ukos_app`` (deep adversarial coverage is later).

No context -> nothing; org A cannot read, insert into, update or delete org B's rows; the grants
(no DELETE, column-level UPDATE) hold for the app role; the app-role write flow works end to end.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from m01_seed import Tenant, seed_tenant, sqlstate
from m02_seed import SHA_A, seed_document
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.core.db import tenant_session

pytestmark = pytest.mark.usefixtures("migrated")

M02_TABLES = ("sources", "documents", "document_versions", "upload_sessions")


class World:
    def __init__(
        self, a: Tenant, b: Tenant, docs: dict[str, tuple[uuid.UUID, uuid.UUID, uuid.UUID]]
    ):
        self.a, self.b, self.docs = a, b, docs


@pytest.fixture(scope="module")
def world(admin_engine: Engine, migrated: None) -> World:
    a = seed_tenant(admin_engine, documents=False)
    b = seed_tenant(admin_engine, documents=False)
    docs = {}
    with admin_engine.begin() as conn:
        for key, t in (("a", a), ("b", b)):
            docs[key] = seed_document(
                conn, org_id=t.org_id, workspace_id=t.workspace_id, created_by=t.owner_id
            )
            conn.execute(
                text(
                    "INSERT INTO upload_sessions (id, org_id, workspace_id, document_id,"
                    " version_id, created_by, filename, content_type, size_bytes, sha256,"
                    " object_key, expires_at) VALUES (:i, :o, :w, :d, :v, :u, 'f', 'a/b', 1, :s,"
                    " :k, now() + interval '1 hour')"
                ),
                {
                    "i": uuid.uuid4(),
                    "o": t.org_id,
                    "w": t.workspace_id,
                    "d": uuid.uuid4(),
                    "v": uuid.uuid4(),
                    "u": t.owner_id,
                    "s": SHA_A,
                    "k": f"k/{uuid.uuid4()}",
                },
            )
    return World(a, b, docs)


def _count(session: Any, table: str, org: uuid.UUID | None = None) -> int:
    if org is None:
        return int(session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
    return int(
        session.execute(
            text(f"SELECT count(*) FROM {table} WHERE org_id = :o"),
            {"o": org},
        ).scalar_one()
    )


@pytest.mark.parametrize("table", M02_TABLES)
def test_no_context_sees_nothing(app_engine: Engine, world: World, table: str) -> None:
    with tenant_session(user_id=None, org_id=None, engine=app_engine) as s:
        assert _count(s, table) == 0
    with tenant_session(user_id=world.a.owner_id, org_id=None, engine=app_engine) as s:
        assert _count(s, table) == 0  # a user context alone does not unlock org data


@pytest.mark.parametrize("table", M02_TABLES)
def test_org_sees_only_its_own_rows(app_engine: Engine, world: World, table: str) -> None:
    with tenant_session(user_id=world.a.owner_id, org_id=world.a.org_id, engine=app_engine) as s:
        assert _count(s, table, world.a.org_id) >= 1
        assert _count(s, table, world.b.org_id) == 0
        assert _count(s, table) == _count(s, table, world.a.org_id)


def test_cross_org_insert_rejected_for_every_table(app_engine: Engine, world: World) -> None:
    a, b = world.a, world.b
    _, doc_b, ver_b = world.docs["b"]
    src_b = world.docs["b"][0]
    statements = {
        "sources": (
            "INSERT INTO sources (id, org_id, workspace_id, kind, name, created_by)"
            " VALUES (:id, :o, :w, 'url', 'evil', :u)"
        ),
        "documents": (
            "INSERT INTO documents (id, org_id, workspace_id, source_id, title, created_by)"
            " VALUES (:id, :o, :w, :src, 'evil', :u)"
        ),
        "document_versions": (
            "INSERT INTO document_versions (id, org_id, workspace_id, document_id,"
            " version_number, original_filename, content_type, size_bytes, sha256, object_key,"
            " created_by) VALUES (:id, :o, :w, :doc, 2, 'f', 'a/b', 1, :sha, :key, :u)"
        ),
        "upload_sessions": (
            "INSERT INTO upload_sessions (id, org_id, workspace_id, document_id, version_id,"
            " created_by, filename, content_type, size_bytes, sha256, object_key, expires_at)"
            " VALUES (:id, :o, :w, :doc, :id, :u, 'f', 'a/b', 1, :sha, :key,"
            " now() + interval '1 hour')"
        ),
    }
    for table, sql in statements.items():
        with (
            pytest.raises(DBAPIError) as exc,
            tenant_session(user_id=a.owner_id, org_id=a.org_id, engine=app_engine) as s,
        ):
            s.execute(
                text(sql),
                {
                    "id": uuid.uuid4(),
                    "o": b.org_id,  # writing into org B while in org A's context
                    "w": b.workspace_id,
                    "u": a.owner_id,
                    "src": src_b,
                    "doc": doc_b,
                    "sha": SHA_A,
                    "key": f"k/{uuid.uuid4()}",
                },
            )
        assert sqlstate(exc.value) == "42501", (table, exc.value)
        assert "row-level security" in str(exc.value.orig), table
    assert ver_b is not None


def test_cross_org_update_touches_nothing(app_engine: Engine, world: World) -> None:
    a, b = world.a, world.b
    _, doc_b, ver_b = world.docs["b"]
    with tenant_session(user_id=a.owner_id, org_id=a.org_id, engine=app_engine) as s:
        r1 = s.execute(text("UPDATE documents SET title = 'pwned' WHERE id = :d"), {"d": doc_b})
        r2 = s.execute(
            text("UPDATE document_versions SET purged_at = now() WHERE id = :v"), {"v": ver_b}
        )
        r3 = s.execute(
            text("UPDATE upload_sessions SET state = 'expired' WHERE org_id = :o"), {"o": b.org_id}
        )
        assert (r1.rowcount, r2.rowcount, r3.rowcount) == (0, 0, 0)  # type: ignore[attr-defined]
    with tenant_session(user_id=b.owner_id, org_id=b.org_id, engine=app_engine) as s:
        assert (
            s.execute(text("SELECT title FROM documents WHERE id = :d"), {"d": doc_b}).scalar_one()
            == "Doc"
        )


def test_update_cannot_move_a_row_to_another_org(app_engine: Engine, world: World) -> None:
    """org_id is not updatable at all (column grant), so a row cannot be re-homed."""
    a, b = world.a, world.b
    _, doc_a, _ = world.docs["a"]
    with (
        pytest.raises(DBAPIError) as exc,
        tenant_session(user_id=a.owner_id, org_id=a.org_id, engine=app_engine) as s,
    ):
        s.execute(
            text("UPDATE documents SET org_id = :b WHERE id = :d"), {"b": b.org_id, "d": doc_a}
        )
    assert sqlstate(exc.value) == "42501"


@pytest.mark.parametrize("table", M02_TABLES)
def test_app_role_cannot_delete(app_engine: Engine, world: World, table: str) -> None:
    with (
        pytest.raises(DBAPIError) as exc,
        tenant_session(user_id=world.a.owner_id, org_id=world.a.org_id, engine=app_engine) as s,
    ):
        s.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": world.a.org_id})
    assert sqlstate(exc.value) == "42501"
    assert "permission denied" in str(exc.value.orig)


@pytest.mark.parametrize("table", M02_TABLES)
def test_app_role_cannot_truncate(app_engine: Engine, world: World, table: str) -> None:
    with (
        pytest.raises(DBAPIError) as exc,
        tenant_session(user_id=world.a.owner_id, org_id=world.a.org_id, engine=app_engine) as s,
    ):
        s.execute(text(f"TRUNCATE {table} CASCADE"))
    assert sqlstate(exc.value) == "42501"


def test_app_role_documents_update_columns(app_engine: Engine, world: World) -> None:
    a = world.a
    _, doc_a, _ = world.docs["a"]
    with tenant_session(user_id=a.owner_id, org_id=a.org_id, engine=app_engine) as s:
        res = s.execute(
            text(
                "UPDATE documents SET title = 'Renamed', updated_at = now(),"
                " deleted_at = now(), deleted_by = :u WHERE id = :d"
            ),
            {"u": a.owner_id, "d": doc_a},
        )
        assert res.rowcount == 1  # type: ignore[attr-defined]
    for column in ("source_id", "created_by", "created_at", "workspace_id"):
        with (
            pytest.raises(DBAPIError) as exc,
            tenant_session(user_id=a.owner_id, org_id=a.org_id, engine=app_engine) as s,
        ):
            s.execute(text(f"UPDATE documents SET {column} = {column} WHERE id = :d"), {"d": doc_a})
        assert sqlstate(exc.value) == "42501", column


def test_app_role_end_to_end_finalize_shape(app_engine: Engine, admin_engine: Engine) -> None:
    """The finalize transaction as the app role: source, document, version, latest, session."""
    t = seed_tenant(admin_engine, documents=False)
    source_id, doc_id, ver_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session_id = uuid.uuid4()
    with tenant_session(user_id=t.owner_id, org_id=t.org_id, engine=app_engine) as s:
        s.execute(
            text(
                "INSERT INTO upload_sessions (id, org_id, workspace_id, document_id, version_id,"
                " created_by, filename, content_type, size_bytes, sha256, object_key, expires_at)"
                " VALUES (:i, :o, :w, :d, :v, :u, 'f.pdf', 'application/pdf', 10, :s, :k,"
                " now() + interval '1 hour')"
            ),
            {
                "i": session_id,
                "o": t.org_id,
                "w": t.workspace_id,
                "d": doc_id,
                "v": ver_id,
                "u": t.owner_id,
                "s": SHA_A,
                "k": f"org/{t.org_id}/doc/{doc_id}/{ver_id}/original",
            },
        )
    with tenant_session(user_id=t.owner_id, org_id=t.org_id, engine=app_engine) as s:
        s.execute(
            text(
                "INSERT INTO sources (id, org_id, workspace_id, kind, name, created_by)"
                " VALUES (:i, :o, :w, 'upload', 'Uploads', :u)"
            ),
            {"i": source_id, "o": t.org_id, "w": t.workspace_id, "u": t.owner_id},
        )
        s.execute(
            text(
                "INSERT INTO documents (id, org_id, workspace_id, source_id, title, created_by)"
                " VALUES (:d, :o, :w, :src, 'f', :u)"
            ),
            {"d": doc_id, "o": t.org_id, "w": t.workspace_id, "src": source_id, "u": t.owner_id},
        )
        s.execute(
            text(
                "INSERT INTO document_versions (id, org_id, workspace_id, document_id,"
                " version_number, original_filename, content_type, size_bytes, sha256, object_key,"
                " created_by)"
                " VALUES (:v, :o, :w, :d, 1, 'f.pdf', 'application/pdf', 10, :s, :k, :u)"
            ),
            {
                "v": ver_id,
                "o": t.org_id,
                "w": t.workspace_id,
                "d": doc_id,
                "s": SHA_A,
                "k": f"org/{t.org_id}/doc/{doc_id}/{ver_id}/original",
                "u": t.owner_id,
            },
        )
        s.execute(
            text("UPDATE documents SET latest_version_id = :v, updated_at = now() WHERE id = :d"),
            {"v": ver_id, "d": doc_id},
        )
        s.execute(
            text(
                "UPDATE upload_sessions SET state = 'finalized', finalized_at = now()"
                " WHERE id = :i AND state = 'pending'"
            ),
            {"i": session_id},
        )
    with tenant_session(user_id=t.owner_id, org_id=t.org_id, engine=app_engine) as s:
        row = s.execute(
            text(
                "SELECT d.latest_version_id, u.state FROM documents d, upload_sessions u"
                " WHERE d.id = :d AND u.id = :i"
            ),
            {"d": doc_id, "i": session_id},
        ).one()
    assert tuple(row) == (ver_id, "finalized")
