"""M02.02.01 acceptance: document versions are immutable (admin AND app role) (ADR-009)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from m01_seed import Tenant, constraint_name, sqlstate
from m02_seed import seed_document
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.core.db import tenant_session

pytestmark = pytest.mark.usefixtures("migrated")

# Every column except purged_at, each with a different (type-correct) value.
IMMUTABLE_UPDATES: dict[str, Any] = {
    "id": uuid.uuid4(),
    "org_id": uuid.uuid4(),
    "workspace_id": uuid.uuid4(),
    "document_id": uuid.uuid4(),
    "version_number": 99,
    "original_filename": "other.pdf",
    "content_type": "text/plain",
    "size_bytes": 12345,
    "sha256": "c" * 64,
    "object_key": "org/other/key",
    "created_by": uuid.uuid4(),
    "created_at": "2001-01-01T00:00:00+00:00",
}


@pytest.fixture
def version(admin_engine: Engine, tenant: Tenant) -> uuid.UUID:
    with admin_engine.begin() as conn:
        _, _, ver = seed_document(
            conn, org_id=tenant.org_id, workspace_id=tenant.workspace_id, created_by=tenant.owner_id
        )
    return ver


def _is_immutable_violation(exc: pytest.ExceptionInfo[DBAPIError]) -> None:
    assert sqlstate(exc.value) == "42501", exc.value
    assert constraint_name(exc.value) == "document_versions_immutable", exc.value
    assert "immutable" in str(exc.value.orig) or "purged_at" in str(exc.value.orig)


def test_every_column_but_purged_at_is_covered() -> None:
    from app.core.models import DocumentVersion

    columns = {c.name for c in DocumentVersion.__table__.columns}
    assert columns - {"purged_at"} == set(IMMUTABLE_UPDATES)


@pytest.mark.parametrize("column", sorted(IMMUTABLE_UPDATES))
def test_admin_cannot_change_any_column(
    admin_engine: Engine, version: uuid.UUID, column: str
) -> None:
    with pytest.raises(DBAPIError) as exc, admin_engine.begin() as conn:
        conn.execute(
            text(f"UPDATE document_versions SET {column} = :val WHERE id = :v"),
            {"val": IMMUTABLE_UPDATES[column], "v": version},
        )
    _is_immutable_violation(exc)


def test_admin_cannot_change_several_columns_at_once(
    admin_engine: Engine, version: uuid.UUID
) -> None:
    with pytest.raises(DBAPIError) as exc, admin_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE document_versions SET size_bytes = 1, sha256 = :s, purged_at = now()"
                " WHERE id = :v"
            ),
            {"s": "d" * 64, "v": version},
        )
    _is_immutable_violation(exc)
    # ...and the rejected statement left purged_at untouched.
    with admin_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT purged_at FROM document_versions WHERE id = :v"), {"v": version}
            ).scalar_one()
            is None
        )


def test_unchanged_rewrite_is_allowed(admin_engine: Engine, version: uuid.UUID) -> None:
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE document_versions SET size_bytes = size_bytes WHERE id = :v"),
            {"v": version},
        )


def test_purged_at_null_to_timestamp_allowed_once(admin_engine: Engine, version: uuid.UUID) -> None:
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE document_versions SET purged_at = now() WHERE id = :v"), {"v": version}
        )
    with admin_engine.connect() as conn:
        purged = conn.execute(
            text("SELECT purged_at FROM document_versions WHERE id = :v"), {"v": version}
        ).scalar_one()
    assert purged is not None
    # timestamp -> NULL, timestamp -> another timestamp: both rejected.
    for new_value in ("NULL", "now() + interval '1 day'", "now() - interval '1 day'"):
        with pytest.raises(DBAPIError) as exc, admin_engine.begin() as conn:
            conn.execute(
                text(f"UPDATE document_versions SET purged_at = {new_value} WHERE id = :v"),
                {"v": version},
            )
        _is_immutable_violation(exc)
    # Rewriting the same timestamp is a no-op and stays allowed.
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE document_versions SET purged_at = purged_at WHERE id = :v"),
            {"v": version},
        )
    with admin_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT purged_at FROM document_versions WHERE id = :v"), {"v": version}
            ).scalar_one()
            == purged
        )


def test_purged_version_stays_fully_immutable(admin_engine: Engine, version: uuid.UUID) -> None:
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE document_versions SET purged_at = now() WHERE id = :v"), {"v": version}
        )
    with pytest.raises(DBAPIError) as exc, admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE document_versions SET size_bytes = 1 WHERE id = :v"), {"v": version}
        )
    _is_immutable_violation(exc)


# --- the app role ---------------------------------------------------------------------------


def test_app_role_can_set_purged_at_once(
    app_engine: Engine, tenant: Tenant, version: uuid.UUID
) -> None:
    with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
        result = s.execute(
            text("UPDATE document_versions SET purged_at = now() WHERE id = :v"), {"v": version}
        )
        assert result.rowcount == 1  # type: ignore[attr-defined]
    with (
        pytest.raises(DBAPIError) as exc,
        tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s,
    ):
        s.execute(
            text("UPDATE document_versions SET purged_at = NULL WHERE id = :v"), {"v": version}
        )
    _is_immutable_violation(exc)
    with (
        pytest.raises(DBAPIError) as exc,
        tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s,
    ):
        s.execute(
            text("UPDATE document_versions SET purged_at = now() + interval '1 day' WHERE id = :v"),
            {"v": version},
        )
    _is_immutable_violation(exc)


@pytest.mark.parametrize("column", sorted(IMMUTABLE_UPDATES))
def test_app_role_has_no_update_on_other_columns(
    app_engine: Engine, tenant: Tenant, version: uuid.UUID, column: str
) -> None:
    """Column-level grant: even before the trigger, the app role may only touch purged_at."""
    with (
        pytest.raises(DBAPIError) as exc,
        tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s,
    ):
        s.execute(
            text(f"UPDATE document_versions SET {column} = :val WHERE id = :v"),
            {"val": IMMUTABLE_UPDATES[column], "v": version},
        )
    assert sqlstate(exc.value) == "42501"
    assert "permission denied" in str(exc.value.orig)


def test_app_role_cannot_delete_versions(
    app_engine: Engine, tenant: Tenant, version: uuid.UUID
) -> None:
    with (
        pytest.raises(DBAPIError) as exc,
        tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s,
    ):
        s.execute(text("DELETE FROM document_versions WHERE id = :v"), {"v": version})
    assert sqlstate(exc.value) == "42501"
