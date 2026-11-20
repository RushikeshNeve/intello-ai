"""Pre-approve by email (migration 0011): pending entry -> consumed at first VERIFIED sign-in."""

from __future__ import annotations

import uuid

from tenancy_support import Api, assert_denied, assert_success_audit


def _sign_in(api: Api, email: str, *, verified: bool = True) -> uuid.UUID:
    """A brand-new identity (fresh subject) signs in through the real provisioner."""
    suffix = uuid.uuid4().hex[:10]
    token = f"tok-pre-{suffix}"
    api.verifier.register(
        token, subject=f"sub-pre-{suffix}", email=email, name="Newcomer", verified=verified
    )
    res = api.client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text
    return uuid.UUID(res.json()["id"])


def _role(api: Api, org_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
    return api.scalar(
        "SELECT role FROM organization_memberships WHERE org_id = :o AND user_id = :u",
        o=org_id,
        u=user_id,
    )


def test_pending_preapproval_is_consumed_at_first_verified_sign_in(api: Api) -> None:
    t = api.tenant()
    base = f"/v1/orgs/{t.org_id}/members/preapprovals"
    email = f"New.Hire-{uuid.uuid4().hex[:8]}@Tenancy.test"
    res = api.post(base, t.admin, {"email": f"  {email} ", "role": "member"})
    assert res.status == 201, res.body
    assert res.body["status"] == "pending"
    assert res.body["member"] is None
    assert res.body["preapproval"]["email"] == email.lower()
    meta = assert_success_audit(api, res, "org.member.preapproved", actor=t.admin, org_id=t.org_id)
    assert meta == {"email": email.lower(), "to_role": "member"}

    # Repeating replaces the pending entry (one per email).
    res = api.post(base, t.admin, {"email": email, "role": "admin"})
    assert res.status == 201
    listed = api.get(base, t.owner)
    assert [(p["email"], p["role"]) for p in listed.body["items"]] == [(email.lower(), "admin")]

    user_id = _sign_in(api, email)
    assert _role(api, t.org_id, user_id) == "admin"
    assert api.get(base, t.owner).body["items"] == []
    assert (
        api.scalar(
            "SELECT count(*) FROM audit_events WHERE org_id = :o AND actor_user_id = :u"
            " AND event_type = 'org.member.added' AND metadata->>'via' = 'preapproval'",
            o=t.org_id,
            u=user_id,
        )
        == 1
    )
    assert (
        api.scalar(
            "SELECT count(*) FROM org_member_preapprovals"
            " WHERE org_id = :o AND consumed_at IS NULL",
            o=t.org_id,
        )
        == 0
    )


def test_existing_verified_user_is_added_directly(api: Api) -> None:
    t = api.tenant()
    frank = api.user("frank")
    res = api.post(
        f"/v1/orgs/{t.org_id}/members/preapprovals",
        t.owner,
        {"email": frank.email, "role": "member"},
    )
    assert res.status == 201, res.body
    assert res.body["status"] == "member_added"
    assert res.body["member"]["user_id"] == str(frank.id)
    assert _role(api, t.org_id, frank.id) == "member"
    assert api.get(f"/v1/orgs/{t.org_id}/members/preapprovals", t.owner).body["items"] == []
    assert_success_audit(api, res, "org.member.added", actor=t.owner, org_id=t.org_id)


def test_unverified_email_never_consumes_and_is_never_matched(api: Api) -> None:
    t = api.tenant()
    base = f"/v1/orgs/{t.org_id}/members/preapprovals"
    email = f"unverified-{uuid.uuid4().hex[:8]}@tenancy.test"
    # An existing account whose email is unverified is NOT picked up by the direct path.
    squatter = _sign_in(api, email, verified=False)
    res = api.post(base, t.owner, {"email": email, "role": "member"})
    assert (res.status, res.body["status"]) == (201, "pending")
    assert _role(api, t.org_id, squatter) is None
    # Signing in again, still unverified: nothing happens.
    _sign_in(api, email, verified=False)
    assert len(api.get(base, t.owner).body["items"]) == 1
    # The rightful (verified) identity gets in.
    verified = _sign_in(api, email)
    assert _role(api, t.org_id, verified) == "member"
    assert _role(api, t.org_id, squatter) is None


def test_permissions_cross_org_and_owner_grant(api: Api) -> None:
    t = api.tenant()
    base = f"/v1/orgs/{t.org_id}/members/preapprovals"
    body = {"email": f"p-{uuid.uuid4().hex[:8]}@tenancy.test", "role": "member"}
    # Non-admin member: 403; outsider (owner of ANOTHER org): 404 on list, create and delete.
    assert_denied(
        api, api.post(base, t.member, body), 403, action="org.members.manage",
        reason="insufficient_role",
    )  # fmt: skip
    assert_denied(
        api, api.get(base, t.outsider), 404, action="org.members.manage", reason="not_org_member"
    )
    assert_denied(
        api, api.post(base, t.outsider, body), 404, action="org.members.manage",
        reason="not_org_member",
    )  # fmt: skip
    # Admins may not grant owner; owners may.
    assert_denied(
        api, api.post(base, t.admin, {**body, "role": "owner"}), 403,
        action="org.members.manage", reason="insufficient_role",
    )  # fmt: skip
    res = api.post(base, t.owner, {**body, "role": "owner"})
    assert res.status == 201, res.body
    owner_entry = res.body["preapproval"]["id"]
    # ... and an admin can neither see through nor withdraw an owner grant.
    assert_denied(
        api, api.delete(f"{base}/{owner_entry}", t.admin), 403,
        action="org.members.manage", reason="insufficient_role",
    )  # fmt: skip
    # Cross-org delete: the other org's owner cannot delete this org's entry.
    assert_denied(
        api, api.delete(f"{base}/{owner_entry}", t.outsider), 404,
        action="org.members.manage", reason="not_org_member",
    )  # fmt: skip
    # The other org's own path cannot reach it either (id belongs to a different org).
    other = f"/v1/orgs/{t.other_org_id}/members/preapprovals/{owner_entry}"
    assert api.delete(other, t.outsider).status == 404
    assert len(api.get(base, t.owner).body["items"]) == 1
    # Owner removes it; audit; gone.
    res = api.delete(f"{base}/{owner_entry}", t.owner)
    assert res.status == 204
    assert_success_audit(api, res, "org.member.preapproval_removed", actor=t.owner, org_id=t.org_id)
    assert api.get(base, t.owner).body["items"] == []
    assert api.delete(f"{base}/{owner_entry}", t.owner).status == 404


def test_invalid_email_is_rejected(api: Api) -> None:
    t = api.tenant()
    base = f"/v1/orgs/{t.org_id}/members/preapprovals"
    for bad in ("nope", "a@b", "two@@x.com", "a b@x.com", ""):
        res = api.post(base, t.owner, {"email": bad, "role": "member"})
        assert (res.status, res.body["detail"]) == (422, "invalid_request"), bad
