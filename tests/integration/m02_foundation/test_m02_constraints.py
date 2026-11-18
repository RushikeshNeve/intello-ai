"""M02.02.01 acceptance: foreign key and table invariants (admin role, real Postgres)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from m01_seed import Tenant, constraint_name, insert_user, sqlstate
from m02_seed import (
    INSERT_VERSION,
    SHA_A,
    insert_document,
    insert_session,
    insert_source,
    insert_version,
    seed_document,
    version_params,
)
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = pytest.mark.usefixtures("migrated")


def _violation(exc: pytest.ExceptionInfo[DBAPIError], *, name: str, state: str = "23514") -> None:
    assert sqlstate(exc.value) == state, exc.value
    assert constraint_name(exc.value) == name, exc.value


def _source(conn: Connection, t: Tenant) -> uuid.UUID:
    return insert_source(conn, org_id=t.org_id, workspace_id=t.workspace_id, created_by=t.owner_id)


def _doc(conn: Connection, t: Tenant, source: uuid.UUID, **kw: Any) -> uuid.UUID:
    return insert_document(
        conn,
        org_id=t.org_id,
        workspace_id=t.workspace_id,
        source_id=source,
        created_by=t.owner_id,
        **kw,
    )


def _version(conn: Connection, t: Tenant, doc: uuid.UUID, **kw: Any) -> uuid.UUID:
    return insert_version(
        conn,
        org_id=t.org_id,
        workspace_id=t.workspace_id,
        document_id=doc,
        created_by=t.owner_id,
        **kw,
    )


# --- sources ------------------------------------------------------------------------------


def test_one_upload_source_per_workspace(admin_engine: Engine, tenant: Tenant) -> None:
    with admin_engine.begin() as conn:
        _source(conn, tenant)
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        _source(conn, tenant)
    _violation(exc, name="uq_sources_workspace_id_upload", state="23505")


def test_other_source_kinds_are_not_limited(admin_engine: Engine, tenant: Tenant) -> None:
    with admin_engine.begin() as conn:
        for kind in ("url", "url", "connector"):
            insert_source(
                conn,
                org_id=tenant.org_id,
                workspace_id=tenant.workspace_id,
                created_by=tenant.owner_id,
                kind=kind,
            )


def test_upload_source_per_workspace_not_per_org(
    admin_engine: Engine, tenant: Tenant, other_tenant: Tenant
) -> None:
    with admin_engine.begin() as conn:
        _source(conn, tenant)
        _source(conn, other_tenant)


def test_source_kind_check(admin_engine: Engine, tenant: Tenant) -> None:
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        insert_source(
            conn,
            org_id=tenant.org_id,
            workspace_id=tenant.workspace_id,
            created_by=tenant.owner_id,
            kind="ftp",
        )
    _violation(exc, name="ck_sources_kind")


def test_source_workspace_must_belong_to_org(
    admin_engine: Engine, tenant: Tenant, other_tenant: Tenant
) -> None:
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        insert_source(
            conn,
            org_id=other_tenant.org_id,  # workspace of org A named under org B
            workspace_id=tenant.workspace_id,
            created_by=tenant.owner_id,
        )
    _violation(exc, name="fk_sources_workspace", state="23503")


# --- documents ----------------------------------------------------------------------------


@pytest.mark.parametrize("title", ["", " ", "  padded", "padded  ", "\tx", "x" * 256], ids=repr)
def test_document_title_check(admin_engine: Engine, tenant: Tenant, title: str) -> None:
    with admin_engine.begin() as conn:
        source = _source(conn, tenant)
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        _doc(conn, tenant, source, title=title)
    _violation(exc, name="ck_documents_title")


@pytest.mark.parametrize("title", ["x", "x" * 255, "with inner  spaces", "日本語"])
def test_document_title_valid(admin_engine: Engine, tenant: Tenant, title: str) -> None:
    with admin_engine.begin() as conn:
        _doc(conn, tenant, _source(conn, tenant), title=title)


def test_document_deleted_pair_check(admin_engine: Engine, tenant: Tenant) -> None:
    with admin_engine.begin() as conn:
        source = _source(conn, tenant)
        doc = _doc(conn, tenant, source)
    for sql in (
        "UPDATE documents SET deleted_at = now() WHERE id = :d",
        "UPDATE documents SET deleted_by = :u WHERE id = :d",
    ):
        with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
            conn.execute(text(sql), {"d": doc, "u": tenant.owner_id})
        _violation(exc, name="ck_documents_deleted")
    with admin_engine.begin() as conn:  # both at once is fine
        conn.execute(
            text("UPDATE documents SET deleted_at = now(), deleted_by = :u WHERE id = :d"),
            {"d": doc, "u": tenant.owner_id},
        )
        conn.execute(
            text("UPDATE documents SET deleted_at = NULL, deleted_by = NULL WHERE id = :d"),
            {"d": doc},
        )


def test_document_workspace_must_belong_to_org(
    admin_engine: Engine, tenant: Tenant, other_tenant: Tenant
) -> None:
    with admin_engine.begin() as conn:
        source = _source(conn, tenant)
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        insert_document(
            conn,
            org_id=other_tenant.org_id,
            workspace_id=tenant.workspace_id,
            source_id=source,
            created_by=tenant.owner_id,
        )
    _violation(exc, name="fk_documents_workspace", state="23503")


def test_document_source_must_be_in_same_workspace(
    admin_engine: Engine, tenant: Tenant, other_tenant: Tenant
) -> None:
    with admin_engine.begin() as conn:
        foreign_source = _source(conn, other_tenant)
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        _doc(conn, tenant, foreign_source)
    _violation(exc, name="fk_documents_source", state="23503")


def test_latest_version_must_belong_to_same_document(admin_engine: Engine, tenant: Tenant) -> None:
    with admin_engine.begin() as conn:
        source = _source(conn, tenant)
        doc_a = _doc(conn, tenant, source, title="A")
        doc_b = _doc(conn, tenant, source, title="B")
        ver_b = _version(conn, tenant, doc_b)
    # Another document's version, same workspace and org: rejected at COMMIT (deferred FK).
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE documents SET latest_version_id = :v WHERE id = :d"),
            {"v": ver_b, "d": doc_a},
        )
    _violation(exc, name="fk_documents_latest_version", state="23503")
    # Its own version is fine.
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE documents SET latest_version_id = :v WHERE id = :d"),
            {"v": ver_b, "d": doc_b},
        )


def test_latest_version_of_another_orgs_document_rejected(
    admin_engine: Engine, tenant: Tenant, other_tenant: Tenant
) -> None:
    with admin_engine.begin() as conn:
        _, _, foreign_version = seed_document(
            conn,
            org_id=other_tenant.org_id,
            workspace_id=other_tenant.workspace_id,
            created_by=other_tenant.owner_id,
        )
        doc = _doc(conn, tenant, _source(conn, tenant))
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE documents SET latest_version_id = :v WHERE id = :d"),
            {"v": foreign_version, "d": doc},
        )
    _violation(exc, name="fk_documents_latest_version", state="23503")


def test_latest_version_fk_is_deferred_so_document_and_version_insert_in_one_transaction(
    admin_engine: Engine, tenant: Tenant
) -> None:
    doc, ver = uuid.uuid4(), uuid.uuid4()
    with admin_engine.begin() as conn:
        source = _source(conn, tenant)
        # Document first, pointing at a version that does not exist yet.
        conn.execute(
            text(
                "INSERT INTO documents (id, org_id, workspace_id, source_id, title,"
                " latest_version_id, created_by) VALUES (:d, :o, :w, :s, 'Doc', :v, :u)"
            ),
            {
                "d": doc,
                "o": tenant.org_id,
                "w": tenant.workspace_id,
                "s": source,
                "v": ver,
                "u": tenant.owner_id,
            },
        )
        _version(conn, tenant, doc, id=ver)
    with admin_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT latest_version_id FROM documents WHERE id = :d"), {"d": doc}
            ).scalar_one()
            == ver
        )


def test_dangling_latest_version_rejected_at_commit(admin_engine: Engine, tenant: Tenant) -> None:
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        source = _source(conn, tenant)
        conn.execute(
            text(
                "INSERT INTO documents (id, org_id, workspace_id, source_id, title,"
                " latest_version_id, created_by) VALUES (:d, :o, :w, :s, 'Doc', :v, :u)"
            ),
            {
                "d": uuid.uuid4(),
                "o": tenant.org_id,
                "w": tenant.workspace_id,
                "s": source,
                "v": uuid.uuid4(),
                "u": tenant.owner_id,
            },
        )
    _violation(exc, name="fk_documents_latest_version", state="23503")


# --- document_versions --------------------------------------------------------------------


@pytest.fixture
def document(admin_engine: Engine, tenant: Tenant) -> uuid.UUID:
    with admin_engine.begin() as conn:
        return _doc(conn, tenant, _source(conn, tenant))


def test_version_number_unique_per_document(
    admin_engine: Engine, tenant: Tenant, document: uuid.UUID
) -> None:
    with admin_engine.begin() as conn:
        _version(conn, tenant, document, version_number=1)
        _version(conn, tenant, document, version_number=2)
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        _version(conn, tenant, document, version_number=2)
    _violation(exc, name="uq_document_versions_document_id_version_number", state="23505")


def test_same_version_number_in_different_documents_is_fine(
    admin_engine: Engine, tenant: Tenant, document: uuid.UUID
) -> None:
    with admin_engine.begin() as conn:
        other = _doc(conn, tenant, _source_for_workspace(conn, tenant), title="Other")
        _version(conn, tenant, document, version_number=1)
        _version(conn, tenant, other, version_number=1)


def _source_for_workspace(conn: Connection, t: Tenant) -> uuid.UUID:
    return conn.execute(
        text("SELECT id FROM sources WHERE workspace_id = :w AND kind = 'upload'"),
        {"w": t.workspace_id},
    ).scalar_one()


def test_object_key_unique(admin_engine: Engine, tenant: Tenant, document: uuid.UUID) -> None:
    with admin_engine.begin() as conn:
        _version(conn, tenant, document, version_number=1, k="org/x/doc/y/z/original")
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        _version(conn, tenant, document, version_number=2, k="org/x/doc/y/z/original")
    _violation(exc, name="uq_document_versions_object_key", state="23505")


@pytest.mark.parametrize(
    ("override", "constraint"),
    [
        ({"sha": "A" * 64}, "ck_document_versions_sha256"),
        ({"sha": "a" * 63}, "ck_document_versions_sha256"),
        ({"sha": "a" * 65}, "ck_document_versions_sha256"),
        ({"sha": "g" * 64}, "ck_document_versions_sha256"),
        ({"sha": "a" * 64 + "\n"}, "ck_document_versions_sha256"),
        ({"sz": -1}, "ck_document_versions_size_bytes"),
        ({"n": 0}, "ck_document_versions_version_number"),
        ({"n": -3}, "ck_document_versions_version_number"),
        ({"fn": ""}, "ck_document_versions_original_filename"),
        ({"fn": "f" * 256}, "ck_document_versions_original_filename"),
        ({"ct": ""}, "ck_document_versions_content_type"),
        ({"ct": "t" * 256}, "ck_document_versions_content_type"),
        ({"k": ""}, "ck_document_versions_object_key"),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_version_check_constraints(
    admin_engine: Engine,
    tenant: Tenant,
    document: uuid.UUID,
    override: dict[str, Any],
    constraint: str,
) -> None:
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        conn.execute(
            INSERT_VERSION,
            version_params(
                org_id=tenant.org_id,
                workspace_id=tenant.workspace_id,
                document_id=document,
                created_by=tenant.owner_id,
                **override,
            ),
        )
    _violation(exc, name=constraint)


@pytest.mark.parametrize(
    "override", [{"sz": 0}, {"sz": 2**40}, {"n": 1}, {"fn": "f" * 255}, {"ct": "t" * 255}]
)
def test_version_boundary_values_accepted(
    admin_engine: Engine, tenant: Tenant, document: uuid.UUID, override: dict[str, Any]
) -> None:
    with admin_engine.begin() as conn:
        conn.execute(
            INSERT_VERSION,
            version_params(
                org_id=tenant.org_id,
                workspace_id=tenant.workspace_id,
                document_id=document,
                created_by=tenant.owner_id,
                sha=SHA_A,
                **override,
            ),
        )


def test_version_workspace_must_belong_to_org(
    admin_engine: Engine, tenant: Tenant, other_tenant: Tenant, document: uuid.UUID
) -> None:
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        insert_version(
            conn,
            org_id=other_tenant.org_id,  # org B + workspace of org A
            workspace_id=tenant.workspace_id,
            document_id=document,
            created_by=tenant.owner_id,
        )
    _violation(exc, name="fk_document_versions_workspace", state="23503")


def test_version_document_must_be_in_same_workspace(
    admin_engine: Engine, tenant: Tenant, other_tenant: Tenant, document: uuid.UUID
) -> None:
    """Org and workspace of org B, but the document belongs to org A's workspace."""
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        insert_version(
            conn,
            org_id=other_tenant.org_id,
            workspace_id=other_tenant.workspace_id,
            document_id=document,
            created_by=other_tenant.owner_id,
        )
    _violation(exc, name="fk_document_versions_document", state="23503")


def test_version_requires_existing_document(admin_engine: Engine, tenant: Tenant) -> None:
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        _version(conn, tenant, uuid.uuid4())
    _violation(exc, name="fk_document_versions_document", state="23503")


def test_version_creator_must_exist(
    admin_engine: Engine, tenant: Tenant, document: uuid.UUID
) -> None:
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        insert_version(
            conn,
            org_id=tenant.org_id,
            workspace_id=tenant.workspace_id,
            document_id=document,
            created_by=uuid.uuid4(),
        )
    _violation(exc, name="fk_document_versions_created_by", state="23503")


def test_deleting_a_version_that_is_latest_is_blocked(admin_engine: Engine, tenant: Tenant) -> None:
    with admin_engine.begin() as conn:
        _, _, ver = seed_document(
            conn, org_id=tenant.org_id, workspace_id=tenant.workspace_id, created_by=tenant.owner_id
        )
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        conn.execute(text("DELETE FROM document_versions WHERE id = :v"), {"v": ver})
    _violation(exc, name="fk_documents_latest_version", state="23503")


# --- upload_sessions ----------------------------------------------------------------------


def _session(conn: Connection, t: Tenant, **kw: Any) -> uuid.UUID:
    return insert_session(
        conn, org_id=t.org_id, workspace_id=t.workspace_id, created_by=t.owner_id, **kw
    )


def test_session_version_id_unique(admin_engine: Engine, tenant: Tenant) -> None:
    version_id = uuid.uuid4()
    with admin_engine.begin() as conn:
        _session(conn, tenant, version_id=version_id)
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        _session(conn, tenant, version_id=version_id)
    _violation(exc, name="uq_upload_sessions_version_id", state="23505")


@pytest.mark.parametrize(
    ("override", "constraint"),
    [
        ({"state": "bogus", "failure_code": None}, "ck_upload_sessions_state"),
        ({"sha": "A" * 64}, "ck_upload_sessions_sha256"),
        ({"sz": 0}, "ck_upload_sessions_size_bytes"),
        ({"fn": ""}, "ck_upload_sessions_filename"),
        ({"fn": "f" * 256}, "ck_upload_sessions_filename"),
        ({"ct": ""}, "ck_upload_sessions_content_type"),
        ({"key": ""}, "ck_upload_sessions_object_key"),
        ({"state": "failed", "failure_code": None}, "ck_upload_sessions_failure_code"),
        ({"state": "pending", "failure_code": "x"}, "ck_upload_sessions_failure_code"),
        ({"state": "finalized", "finalized_at": None}, "ck_upload_sessions_finalized_at"),
        (
            {"state": "pending", "finalized_at": "2026-01-01T00:00:00Z"},
            "ck_upload_sessions_finalized_at",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_session_check_constraints(
    admin_engine: Engine, tenant: Tenant, override: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        _session(conn, tenant, **override)
    _violation(exc, name=constraint)


def test_session_workspace_must_belong_to_org(
    admin_engine: Engine, tenant: Tenant, other_tenant: Tenant
) -> None:
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        insert_session(
            conn,
            org_id=other_tenant.org_id,
            workspace_id=tenant.workspace_id,
            created_by=tenant.owner_id,
        )
    _violation(exc, name="fk_upload_sessions_workspace", state="23503")


def test_session_must_expire_after_creation(admin_engine: Engine, tenant: Tenant) -> None:
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO upload_sessions (id, org_id, workspace_id, document_id, version_id,"
                " created_by, filename, content_type, size_bytes, sha256, object_key, expires_at)"
                " VALUES (:i, :o, :w, :d, :v, :u, 'f', 'a/b', 1, :s, 'k', now() - interval '1 h')"
            ),
            {
                "i": uuid.uuid4(),
                "o": tenant.org_id,
                "w": tenant.workspace_id,
                "d": uuid.uuid4(),
                "v": uuid.uuid4(),
                "u": tenant.owner_id,
                "s": SHA_A,
            },
        )
    _violation(exc, name="ck_upload_sessions_expires_after_created")


@pytest.mark.parametrize("terminal", ["finalized", "failed", "expired"])
def test_session_state_moves_forward_only(
    admin_engine: Engine, tenant: Tenant, terminal: str
) -> None:
    with admin_engine.begin() as conn:
        sid = _session(conn, tenant)
    updates = {
        "finalized": "state = 'finalized', finalized_at = now()",
        "failed": "state = 'failed', failure_code = 'checksum_mismatch'",
        "expired": "state = 'expired'",
    }
    with admin_engine.begin() as conn:  # pending -> terminal is allowed
        conn.execute(
            text(f"UPDATE upload_sessions SET {updates[terminal]} WHERE id = :i"), {"i": sid}
        )
    for other in ("pending", "finalized", "failed", "expired"):
        if other == terminal:
            continue
        back = {
            "pending": "state = 'pending', failure_code = NULL, finalized_at = NULL",
            "finalized": "state = 'finalized', failure_code = NULL, finalized_at = now()",
            "failed": "state = 'failed', failure_code = 'x', finalized_at = NULL",
            "expired": "state = 'expired', failure_code = NULL, finalized_at = NULL",
        }[other]
        with pytest.raises(DBAPIError) as exc, admin_engine.begin() as conn:
            conn.execute(text(f"UPDATE upload_sessions SET {back} WHERE id = :i"), {"i": sid})
        _violation(exc, name="upload_sessions_state_forward_only")
    with admin_engine.begin() as conn:  # a no-op rewrite of the same terminal state is harmless
        conn.execute(text("UPDATE upload_sessions SET state = state WHERE id = :i"), {"i": sid})


def test_pending_session_can_be_updated_without_leaving_pending(
    admin_engine: Engine, tenant: Tenant
) -> None:
    with admin_engine.begin() as conn:
        sid = _session(conn, tenant)
        conn.execute(text("UPDATE upload_sessions SET state = 'pending' WHERE id = :i"), {"i": sid})


# --- cross-cutting ------------------------------------------------------------------------


def test_document_creator_must_exist(admin_engine: Engine, tenant: Tenant) -> None:
    """A document creator that is not a user is rejected (plain FK, sanity check)."""
    with admin_engine.begin() as conn:
        source = _source(conn, tenant)
        ghost = uuid.uuid4()
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        insert_document(
            conn,
            org_id=tenant.org_id,
            workspace_id=tenant.workspace_id,
            source_id=source,
            created_by=ghost,
        )
    _violation(exc, name="fk_documents_created_by", state="23503")
    with admin_engine.begin() as conn:
        user = insert_user(conn)
        insert_document(
            conn,
            org_id=tenant.org_id,
            workspace_id=tenant.workspace_id,
            source_id=source,
            created_by=user,
        )
