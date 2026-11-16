"""Grant, revoke and expiry semantics on real PostgreSQL, as the app role under RLS (M01.03.03).

Every session here is a :func:`tenant_session` on the ``ukos_app`` engine (NOBYPASSRLS); the admin
engine is used only to seed and to inspect rows.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from grants_seed import GrantTenant
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.core.db import tenant_session
from app.modules.authorization.grants import (
    Grantee,
    GrantNotFoundError,
    GrantScopeError,
    InvalidGrantError,
    ResourceRef,
    active_permissions,
    get_grant,
    grant,
    list_grants,
    revoke,
)
from app.modules.authorization.policy import (
    Action,
    GrantPermission,
    OrgRole,
    SubjectContext,
    WorkspaceRole,
    decide,
)

pytestmark = pytest.mark.usefixtures("migrated")

R, W, M = GrantPermission.READ, GrantPermission.WRITE, GrantPermission.MANAGE


def _doc(t: GrantTenant) -> ResourceRef:
    return ResourceRef(org_id=t.org_id, workspace_id=t.workspace_id, resource_id=uuid.uuid4())


def _perms(
    engine: Engine,
    t: GrantTenant,
    doc: ResourceRef,
    user_id: uuid.UUID,
    role: WorkspaceRole | None = None,
    *,
    now: datetime | None = None,
    org_id: uuid.UUID | None = None,
) -> frozenset[GrantPermission]:
    with tenant_session(user_id=user_id, org_id=org_id or t.org_id, engine=engine) as s:
        return active_permissions(s, resource=doc, user_id=user_id, workspace_role=role, now=now)


def _grant(
    engine: Engine,
    t: GrantTenant,
    doc: ResourceRef,
    grantee: Grantee,
    permission: GrantPermission,
    *,
    expires_at: datetime | None = None,
) -> uuid.UUID:
    with tenant_session(user_id=t.owner_id, org_id=t.org_id, engine=engine) as s:
        record = grant(
            s,
            resource=doc,
            grantee=grantee,
            permission=permission,
            granted_by=t.owner_id,
            expires_at=expires_at,
        )
        return record.id


def _row(admin_engine: Engine, grant_id: uuid.UUID) -> dict[str, object]:
    with admin_engine.connect() as conn:
        return dict(
            conn.execute(text("SELECT * FROM resource_grants WHERE id = :id"), {"id": grant_id})
            .one()
            ._mapping
        )


def test_grant_creates_active_row(
    app_engine: Engine, admin_engine: Engine, tenant: GrantTenant
) -> None:
    doc = _doc(tenant)
    assert _perms(app_engine, tenant, doc, tenant.member_id) == frozenset()

    with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
        record = grant(
            s,
            resource=doc,
            grantee=Grantee.user(tenant.member_id),
            permission=R,
            granted_by=tenant.owner_id,
        )
    assert record.created is True
    assert record.resource == doc
    assert record.grantee == Grantee.user(tenant.member_id)
    assert (record.permission, record.granted_by) == (R, tenant.owner_id)
    assert record.expires_at is None
    assert record.revoked_at is None
    assert record.revoked_by is None
    assert record.created_at.tzinfo is not None
    assert record.audit_metadata()["grantee_id"] == str(tenant.member_id)

    row = _row(admin_engine, record.id)
    assert row["org_id"] == tenant.org_id
    assert row["grantee_type"] == "user"
    assert row["grantee_id"] == str(tenant.member_id)
    assert row["permission"] == "read"
    assert _perms(app_engine, tenant, doc, tenant.member_id) == frozenset({R})
    # A different document is unaffected.
    assert _perms(app_engine, tenant, _doc(tenant), tenant.member_id) == frozenset()


@pytest.mark.parametrize(
    ("permission", "expected"),
    [(R, {R}), (W, {R, W}), (M, {R, W, M})],
)
def test_implication_on_read(
    app_engine: Engine,
    tenant: GrantTenant,
    permission: GrantPermission,
    expected: set[GrantPermission],
) -> None:
    doc = _doc(tenant)
    _grant(app_engine, tenant, doc, Grantee.user(tenant.member_id), permission)
    assert _perms(app_engine, tenant, doc, tenant.member_id) == frozenset(expected)


def test_revoke_makes_inactive_and_is_idempotent(
    app_engine: Engine, admin_engine: Engine, tenant: GrantTenant
) -> None:
    doc = _doc(tenant)
    grant_id = _grant(app_engine, tenant, doc, Grantee.user(tenant.member_id), W)
    assert _perms(app_engine, tenant, doc, tenant.member_id) == frozenset({R, W})

    with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
        assert revoke(s, grant_id=grant_id, revoked_by=tenant.owner_id) is True
    first = _row(admin_engine, grant_id)
    assert first["revoked_at"] is not None
    assert first["revoked_by"] == tenant.owner_id
    assert _perms(app_engine, tenant, doc, tenant.member_id) == frozenset()

    # Second revoke, by someone else and later: no change, returns False.
    time.sleep(0.05)
    with tenant_session(user_id=tenant.editor_id, org_id=tenant.org_id, engine=app_engine) as s:
        assert revoke(s, grant_id=grant_id, revoked_by=tenant.editor_id) is False
        record = get_grant(s, grant_id=grant_id)
    second = _row(admin_engine, grant_id)
    assert (second["revoked_at"], second["revoked_by"]) == (first["revoked_at"], tenant.owner_id)
    assert record is not None
    assert record.revoked_at == first["revoked_at"]

    # The row still exists (revoke is an update, never a delete) and is listed only as inactive.
    with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
        assert list_grants(s, resource=doc) == []
        assert [g.id for g in list_grants(s, resource=doc, include_inactive=True)] == [grant_id]


def test_regrant_after_revoke_creates_new_row(app_engine: Engine, tenant: GrantTenant) -> None:
    doc = _doc(tenant)
    grantee = Grantee.user(tenant.member_id)
    first = _grant(app_engine, tenant, doc, grantee, R)
    with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
        assert revoke(s, grant_id=first, revoked_by=tenant.owner_id)
    second = _grant(app_engine, tenant, doc, grantee, R)
    assert second != first
    assert _perms(app_engine, tenant, doc, tenant.member_id) == frozenset({R})


def test_revoke_unknown_grant_raises(app_engine: Engine, tenant: GrantTenant) -> None:
    with (
        tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s,
        pytest.raises(GrantNotFoundError),
    ):
        revoke(s, grant_id=uuid.uuid4(), revoked_by=tenant.owner_id)


def test_app_role_cannot_delete_grants(app_engine: Engine, tenant: GrantTenant) -> None:
    doc = _doc(tenant)
    grant_id = _grant(app_engine, tenant, doc, Grantee.user(tenant.member_id), R)
    with (
        tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s,
        pytest.raises(DBAPIError) as exc_info,
    ):
        s.execute(text("DELETE FROM resource_grants WHERE id = :id"), {"id": grant_id})
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"


def test_expires_at_in_past_or_naive_rejected(
    app_engine: Engine, admin_engine: Engine, tenant: GrantTenant
) -> None:
    doc = _doc(tenant)
    for bad in (datetime.now(UTC) - timedelta(seconds=1), datetime(2999, 1, 1)):
        with (
            tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s,
            pytest.raises(InvalidGrantError),
        ):
            grant(
                s,
                resource=doc,
                grantee=Grantee.user(tenant.member_id),
                permission=R,
                granted_by=tenant.owner_id,
                expires_at=bad,
            )
    with admin_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM resource_grants WHERE resource_id = :r"),
            {"r": doc.resource_id},
        ).scalar_one()
    assert count == 0


def test_grant_expires_without_any_update(
    app_engine: Engine, admin_engine: Engine, tenant: GrantTenant
) -> None:
    doc = _doc(tenant)
    expires_at = datetime.now(UTC) + timedelta(seconds=2)
    grant_id = _grant(
        app_engine, tenant, doc, Grantee.user(tenant.member_id), W, expires_at=expires_at
    )
    before = _row(admin_engine, grant_id)
    assert _perms(app_engine, tenant, doc, tenant.member_id) == frozenset({R, W})

    # Wait on the database clock, not the host clock, so container clock skew can't flake this.
    deadline = time.monotonic() + 10
    while True:
        with admin_engine.connect() as conn:
            if conn.execute(text("SELECT now() > :e"), {"e": expires_at}).scalar_one():
                break
        assert time.monotonic() < deadline, "database clock never passed expires_at"
        time.sleep(0.25)

    assert _perms(app_engine, tenant, doc, tenant.member_id) == frozenset()
    assert _row(admin_engine, grant_id) == before  # nothing was written
    with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
        assert list_grants(s, resource=doc) == []


def test_injected_now_evaluates_expiry(app_engine: Engine, tenant: GrantTenant) -> None:
    doc = _doc(tenant)
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    _grant(app_engine, tenant, doc, Grantee.user(tenant.member_id), R, expires_at=expires_at)
    user = tenant.member_id
    assert _perms(app_engine, tenant, doc, user, now=expires_at - timedelta(seconds=1)) == {R}
    assert _perms(app_engine, tenant, doc, user, now=expires_at) == frozenset()
    assert _perms(app_engine, tenant, doc, user, now=expires_at + timedelta(days=1)) == frozenset()


def test_injected_now_still_honours_revocation(app_engine: Engine, tenant: GrantTenant) -> None:
    doc = _doc(tenant)
    grant_id = _grant(app_engine, tenant, doc, Grantee.user(tenant.member_id), R)
    with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
        revoke(s, grant_id=grant_id, revoked_by=tenant.owner_id)
    past = datetime.now(UTC) - timedelta(days=1)
    assert _perms(app_engine, tenant, doc, tenant.member_id, now=past) == frozenset()


def test_workspace_role_grantee_matches_only_that_role(
    app_engine: Engine, tenant: GrantTenant
) -> None:
    doc = _doc(tenant)
    _grant(app_engine, tenant, doc, Grantee.role(WorkspaceRole.EDITOR), M)
    t = tenant
    assert _perms(app_engine, t, doc, t.editor_id, WorkspaceRole.EDITOR) == {R, W, M}
    assert _perms(app_engine, t, doc, t.viewer_id, WorkspaceRole.VIEWER) == frozenset()
    assert _perms(app_engine, t, doc, t.owner_id, WorkspaceRole.ADMIN) == frozenset()
    # Without the role, the editor has no grant either.
    assert _perms(app_engine, t, doc, t.editor_id, None) == frozenset()
    assert _perms(app_engine, t, doc, t.member_id, None) == frozenset()


def test_user_and_role_grants_combine(app_engine: Engine, tenant: GrantTenant) -> None:
    doc = _doc(tenant)
    _grant(app_engine, tenant, doc, Grantee.role(WorkspaceRole.VIEWER), R)
    _grant(app_engine, tenant, doc, Grantee.user(tenant.viewer_id), W)
    t = tenant
    assert _perms(app_engine, t, doc, t.viewer_id, WorkspaceRole.VIEWER) == {R, W}
    assert _perms(app_engine, t, doc, t.viewer_id, None) == {R, W}


def test_user_grant_does_not_leak_to_other_users(app_engine: Engine, tenant: GrantTenant) -> None:
    doc = _doc(tenant)
    _grant(app_engine, tenant, doc, Grantee.user(tenant.member_id), M)
    assert _perms(app_engine, tenant, doc, tenant.editor_id, WorkspaceRole.EDITOR) == frozenset()


def test_duplicate_grant_is_idempotent(
    app_engine: Engine, admin_engine: Engine, tenant: GrantTenant
) -> None:
    doc = _doc(tenant)
    grantee = Grantee.user(tenant.member_id)
    with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
        first = grant(s, resource=doc, grantee=grantee, permission=R, granted_by=tenant.owner_id)
        again = grant(
            s, resource=doc, grantee=grantee, permission="read", granted_by=tenant.owner_id
        )
    with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
        later = grant(
            s,
            resource=doc,
            grantee=grantee,
            permission=R,
            granted_by=tenant.editor_id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        other_perm = grant(
            s, resource=doc, grantee=grantee, permission=W, granted_by=tenant.owner_id
        )
    assert first.created
    assert not again.created
    assert not later.created
    assert again == first
    assert later == first
    assert later.expires_at is None
    assert later.granted_by == tenant.owner_id
    assert other_perm.created
    assert other_perm.id != first.id
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT permission FROM resource_grants WHERE resource_id = :r ORDER BY 1"),
            {"r": doc.resource_id},
        ).scalars()
        assert list(rows) == ["read", "write"]


def test_concurrent_duplicate_grants_produce_one_row(
    app_engine: Engine, admin_engine: Engine, tenant: GrantTenant
) -> None:
    doc = _doc(tenant)
    grantee = Grantee.role(WorkspaceRole.VIEWER)
    first_granted = threading.Event()
    release_first = threading.Event()
    results: dict[str, uuid.UUID] = {}
    errors: list[BaseException] = []

    def first() -> None:
        try:
            with tenant_session(
                user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine
            ) as s:
                rec = grant(
                    s, resource=doc, grantee=grantee, permission=R, granted_by=tenant.owner_id
                )
                results["first"] = rec.id
                first_granted.set()
                assert release_first.wait(10)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the main thread
            errors.append(exc)
            first_granted.set()

    def second() -> None:
        try:
            with tenant_session(
                user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine
            ) as s:
                rec = grant(
                    s, resource=doc, grantee=grantee, permission=R, granted_by=tenant.owner_id
                )
                results["second"] = rec.id
        except BaseException as exc:  # noqa: BLE001 - surfaced by the main thread
            errors.append(exc)

    t1 = threading.Thread(target=first)
    t1.start()
    assert first_granted.wait(10)
    t2 = threading.Thread(target=second)
    t2.start()
    t2.join(0.5)
    assert t2.is_alive(), "second grant should wait on the first transaction's advisory lock"
    release_first.set()
    t1.join(10)
    t2.join(10)
    assert not errors, errors
    assert results["first"] == results["second"]
    with admin_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM resource_grants WHERE resource_id = :r"),
            {"r": doc.resource_id},
        ).scalar_one()
    assert count == 1


def test_grants_never_cross_org(
    app_engine: Engine, tenant: GrantTenant, other_tenant: GrantTenant
) -> None:
    a, b = tenant, other_tenant
    doc_b = _doc(b)
    grant_b = _grant(app_engine, b, doc_b, Grantee.user(b.member_id), M)
    _grant(app_engine, b, doc_b, Grantee.role(WorkspaceRole.EDITOR), M)
    assert _perms(app_engine, b, doc_b, b.member_id) == {R, W, M}

    # Org A context asking about org B's document (true ref, or same ids relabelled as org A).
    relabelled = ResourceRef(
        org_id=a.org_id, workspace_id=b.workspace_id, resource_id=doc_b.resource_id
    )
    for ref in (doc_b, relabelled):
        assert _perms(app_engine, a, ref, b.member_id, org_id=a.org_id) == frozenset()
        assert _perms(app_engine, a, ref, a.editor_id, WorkspaceRole.EDITOR) == frozenset()

    with tenant_session(user_id=a.owner_id, org_id=a.org_id, engine=app_engine) as s:
        assert get_grant(s, grant_id=grant_b) is None
        assert list_grants(s, resource=doc_b, include_inactive=True) == []
        with pytest.raises(GrantNotFoundError):
            revoke(s, grant_id=grant_b, revoked_by=a.owner_id)
        with pytest.raises(GrantScopeError):
            grant(
                s,
                resource=doc_b,
                grantee=Grantee.user(a.member_id),
                permission=R,
                granted_by=a.owner_id,
            )

    # No org context at all: nothing is visible and nothing can be granted.
    with tenant_session(user_id=b.owner_id, org_id=None, engine=app_engine) as s:
        assert active_permissions(s, resource=doc_b, user_id=b.member_id, workspace_role=None) == (
            frozenset()
        )
        with pytest.raises(GrantScopeError):
            grant(
                s,
                resource=doc_b,
                grantee=Grantee.user(b.member_id),
                permission=R,
                granted_by=b.owner_id,
            )

    # Org B's grant is untouched.
    assert _perms(app_engine, b, doc_b, b.member_id) == {R, W, M}


def test_rls_rejects_grant_row_for_another_org(app_engine: Engine, tenant: GrantTenant) -> None:
    """Defense in depth: even bypassing grant()'s own check, RLS refuses the insert."""
    other_org = uuid.uuid4()
    with (
        tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s,
        pytest.raises(DBAPIError) as exc_info,
    ):
        s.execute(
            text(
                "INSERT INTO resource_grants (id, org_id, workspace_id, resource_type, resource_id,"
                " grantee_type, grantee_id, permission, granted_by)"
                " VALUES (:id, :o, :w, 'document', :r, 'workspace_role', 'viewer', 'read', :u)"
            ),
            {
                "id": uuid.uuid4(),
                "o": other_org,
                "w": tenant.workspace_id,
                "r": uuid.uuid4(),
                "u": tenant.owner_id,
            },
        )
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"


def test_policy_decide_with_active_permissions(app_engine: Engine, tenant: GrantTenant) -> None:
    doc = _doc(tenant)
    user = tenant.member_id  # org member, no workspace membership

    def subject() -> SubjectContext:
        return SubjectContext(
            user_id=user,
            org_role=OrgRole.MEMBER,
            workspace_role=None,
            grants=_perms(app_engine, tenant, doc, user),
        )

    before = decide(subject(), Action.DOCUMENT_READ)
    assert (before.allowed, before.reason) == (False, "not_workspace_member")

    grant_id = _grant(app_engine, tenant, doc, Grantee.user(user), R)
    ctx = subject()
    allowed = decide(ctx, Action.DOCUMENT_READ)
    assert (allowed.allowed, allowed.reason) == (True, "allowed_by_grant")
    write = decide(ctx, Action.DOCUMENT_WRITE)
    assert (write.allowed, write.reason) == (False, "insufficient_role")

    with tenant_session(user_id=tenant.owner_id, org_id=tenant.org_id, engine=app_engine) as s:
        assert revoke(s, grant_id=grant_id, revoked_by=tenant.owner_id)
    after = decide(subject(), Action.DOCUMENT_READ)
    assert (after.allowed, after.reason) == (False, "not_workspace_member")


def test_policy_decide_viewer_gets_write_via_role_grant(
    app_engine: Engine, tenant: GrantTenant
) -> None:
    doc = _doc(tenant)
    viewer = SubjectContext(
        user_id=tenant.viewer_id, org_role=OrgRole.MEMBER, workspace_role=WorkspaceRole.VIEWER
    )
    assert not decide(viewer, Action.DOCUMENT_WRITE).allowed

    _grant(app_engine, tenant, doc, Grantee.role(WorkspaceRole.VIEWER), W)
    grants = _perms(app_engine, tenant, doc, tenant.viewer_id, WorkspaceRole.VIEWER)
    with_grant = SubjectContext(
        user_id=tenant.viewer_id,
        org_role=OrgRole.MEMBER,
        workspace_role=WorkspaceRole.VIEWER,
        grants=grants,
    )
    write = decide(with_grant, Action.DOCUMENT_WRITE)
    assert (write.allowed, write.reason) == (True, "allowed_by_grant")
    assert not decide(with_grant, Action.DOCUMENT_MANAGE).allowed
