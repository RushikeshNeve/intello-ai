"""Partial unique index on ACTIVE resource grants (DC-015 follow-up)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from m01_seed import Tenant, constraint_name, sqlstate
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.usefixtures("migrated")


def _grant(conn: Connection, t: Tenant, resource: uuid.UUID, **kw: Any) -> uuid.UUID:
    grant_id = uuid.uuid4()
    params = {
        "id": grant_id,
        "o": t.org_id,
        "w": t.workspace_id,
        "r": resource,
        "gt": "user",
        "gi": str(t.owner_id),
        "p": "read",
        "u": t.owner_id,
    }
    params.update(kw)
    conn.execute(
        text(
            "INSERT INTO resource_grants (id, org_id, workspace_id, resource_type, resource_id,"
            " grantee_type, grantee_id, permission, granted_by)"
            " VALUES (:id, :o, :w, 'document', :r, :gt, :gi, :p, :u)"
        ),
        params,
    )
    return grant_id


def test_duplicate_active_grant_rejected(admin_engine: Engine, tenant: Tenant) -> None:
    resource = uuid.uuid4()
    with admin_engine.begin() as conn:
        _grant(conn, tenant, resource)
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        _grant(conn, tenant, resource)
    assert sqlstate(exc.value) == "23505"
    assert constraint_name(exc.value) == "uq_resource_grants_active"


def test_distinct_permission_grantee_or_resource_is_fine(
    admin_engine: Engine, tenant: Tenant
) -> None:
    resource = uuid.uuid4()
    with admin_engine.begin() as conn:
        _grant(conn, tenant, resource)
        _grant(conn, tenant, resource, p="write")
        _grant(conn, tenant, resource, gt="workspace_role", gi="viewer")
        _grant(conn, tenant, uuid.uuid4())


def test_revoked_grant_frees_the_slot(admin_engine: Engine, tenant: Tenant) -> None:
    resource = uuid.uuid4()
    with admin_engine.begin() as conn:
        first = _grant(conn, tenant, resource)
        conn.execute(
            text("UPDATE resource_grants SET revoked_at = now(), revoked_by = :u WHERE id = :i"),
            {"u": tenant.owner_id, "i": first},
        )
        _grant(conn, tenant, resource)  # a new active grant
    with pytest.raises(IntegrityError) as exc, admin_engine.begin() as conn:
        _grant(conn, tenant, resource)  # a second active one is rejected again
    assert constraint_name(exc.value) == "uq_resource_grants_active"
    # Many revoked rows for the same key are allowed.
    with admin_engine.begin() as conn:
        for _ in range(3):
            gid = _grant(conn, tenant, resource, p="manage")
            conn.execute(
                text("UPDATE resource_grants SET revoked_at = now() WHERE id = :i"), {"i": gid}
            )
