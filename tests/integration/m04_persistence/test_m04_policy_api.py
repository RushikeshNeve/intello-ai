"""M04.02.01: per-org AI policy storage and the GET/PUT /v1/orgs/{o}/ai-policy route."""

from __future__ import annotations

import uuid

import pytest
from m04_support import ChatWorld
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from tenancy_support import Api, Tenant, assert_denied

from app.core.db import tenant_session
from app.modules.ai import policy_store


def _url(t: Tenant) -> str:
    return f"/v1/orgs/{t.org_id}/ai-policy"


def test_default_policy_is_local_only_and_any_member_may_read(api: Api) -> None:
    t = api.tenant()
    for user in (t.owner, t.admin, t.member):
        res = api.get(_url(t), user)
        assert res.status == 200, res.body
        assert res.body["allow_external"] is False
        assert res.body["allowed_providers"] == ["local"]
        assert res.body["is_default"] is True
        assert res.body["updated_at"] is None
        assert res.body["server_allows_external"] is False
    assert api.scalar("SELECT count(*) FROM ai_policies WHERE org_id = :o", o=t.org_id) == 0


def test_owner_and_admin_write_and_it_is_audited(api: Api) -> None:
    t = api.tenant()
    res = api.put(
        _url(t), t.owner, {"allow_external": True, "allowed_providers": ["openai", "local"]}
    )
    assert res.status == 200, res.body
    assert res.body["allow_external"] is True
    assert res.body["allowed_providers"] == ["local", "openai"]
    assert res.body["is_default"] is False
    assert res.body["updated_by"] == str(t.owner.id)
    rows = api.audit(res.request_id)
    assert [r["event_type"] for r in rows] == ["org.ai_policy.updated"]
    assert (rows[0]["actor_user_id"], rows[0]["org_id"], rows[0]["outcome"]) == (
        t.owner.id,
        t.org_id,
        "success",
    )
    assert rows[0]["target_type"] == "ai_policy"
    assert rows[0]["target_id"] == str(t.org_id)
    assert rows[0]["metadata"] == {
        "from_allow_external": False,
        "from_providers": ["local"],
        "allow_external": True,
        "providers": ["local", "openai"],
    }
    # An admin can narrow it again; the read sees the stored row.
    res2 = api.put(_url(t), t.admin, {"allow_external": False, "allowed_providers": ["local"]})
    assert res2.status == 200, res2.body
    assert res2.body["updated_by"] == str(t.admin.id)
    got = api.get(_url(t), t.member)
    assert (got.body["allow_external"], got.body["allowed_providers"], got.body["is_default"]) == (
        False,
        ["local"],
        False,
    )
    assert api.scalar("SELECT count(*) FROM ai_policies WHERE org_id = :o", o=t.org_id) == 1


def test_plain_member_cannot_write_and_nothing_changes(api: Api) -> None:
    t = api.tenant()
    res = api.put(
        _url(t), t.member, {"allow_external": True, "allowed_providers": ["local", "openai"]}
    )
    assert_denied(api, res, 403, action="org.update", reason="insufficient_role")
    assert api.scalar("SELECT count(*) FROM ai_policies WHERE org_id = :o", o=t.org_id) == 0


def test_outsider_gets_a_concealed_404_on_read_and_write(api: Api) -> None:
    t = api.tenant()
    res = api.get(_url(t), t.outsider)
    assert_denied(api, res, 404, action="org.read", reason="not_org_member")
    res = api.put(_url(t), t.outsider, {"allow_external": False, "allowed_providers": ["local"]})
    assert_denied(api, res, 404, action="org.update", reason="not_org_member")
    unknown = api.get(f"/v1/orgs/{uuid.uuid4()}/ai-policy", t.owner)
    assert (unknown.status, unknown.body) == (404, {"detail": "not_found"})
    assert api.get(_url(t), None).status in (401, 403)


@pytest.mark.parametrize(
    "body",
    [
        {"allow_external": False, "allowed_providers": ["local", "openai"]},  # external not allowed
        {"allow_external": True, "allowed_providers": ["claude"]},
        {"allow_external": True, "allowed_providers": []},
        {"allow_external": True},
        {"allowed_providers": ["local"]},
        {"allow_external": True, "allowed_providers": ["local"], "extra": 1},
        {"allow_external": "maybe", "allowed_providers": ["local"]},
    ],
)
def test_invalid_policies_are_422_and_store_nothing(api: Api, body: dict[str, object]) -> None:
    t = api.tenant()
    res = api.put(_url(t), t.owner, body)
    assert res.status == 422, res.body
    assert api.scalar("SELECT count(*) FROM ai_policies WHERE org_id = :o", o=t.org_id) == 0
    assert (
        api.scalar("SELECT count(*) FROM audit_events WHERE request_id = :r", r=res.request_id) == 0
    )


def test_openapi_lists_the_route(api: Api) -> None:
    spec = api.client.get("/openapi.json").json()
    path = spec["paths"]["/v1/orgs/{org_id}/ai-policy"]
    assert set(path) == {"get", "put"}


# ----------------------------------------------------------------- store + RLS (DB level)


def test_store_default_and_roundtrip_under_rls(
    app_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    with tenant_session(user_id=world.member, org_id=world.org, engine=app_engine) as s:
        assert policy_store.load_policy(s, world.org) == policy_store.ModelPolicy()
        assert policy_store.load_stored_policy(s, world.org).is_default
    with tenant_session(user_id=world.admin, org_id=world.org, engine=app_engine) as s:
        saved = policy_store.save_policy(
            s, world.org, allow_external=True, allowed_providers=["openai"], updated_by=world.admin
        )
    assert saved.policy == policy_store.ModelPolicy(True, frozenset({"openai"}))
    with tenant_session(user_id=world.member, org_id=world.org, engine=app_engine) as s:
        assert policy_store.load_policy(s, world.org) == saved.policy
    with admin_engine.begin() as conn:
        conn.execute(text("DELETE FROM ai_policies WHERE org_id = :o"), {"o": world.org})


def test_rls_lets_only_org_admins_write_and_only_the_current_org_read(
    app_engine: Engine, admin_engine: Engine, world: ChatWorld
) -> None:
    for user in (world.member, world.owner_b):
        with (
            pytest.raises(DBAPIError),
            tenant_session(user_id=user, org_id=world.org, engine=app_engine) as s,
        ):
            s.execute(
                text("INSERT INTO ai_policies (org_id, updated_by) VALUES (:o, :u)"),
                {"o": world.org, "u": user},
            )
    with tenant_session(user_id=world.owner, org_id=world.org, engine=app_engine) as s:
        policy_store.save_policy(
            s, world.org, allow_external=False, allowed_providers=["local"], updated_by=world.owner
        )
    # A member's UPDATE matches no row (RLS), it does not raise and changes nothing.
    with tenant_session(user_id=world.member, org_id=world.org, engine=app_engine) as s:
        res = s.execute(
            text(
                "UPDATE ai_policies SET allow_external = true, allowed_providers = '{local,openai}'"
            )
        )
        assert res.rowcount == 0  # type: ignore[attr-defined]
    # An admin of org A cannot write org B's policy (context is org A, row says org B).
    with (
        pytest.raises(DBAPIError),
        tenant_session(user_id=world.owner, org_id=world.org, engine=app_engine) as s,
    ):
        s.execute(
            text("INSERT INTO ai_policies (org_id, updated_by) VALUES (:o, :u)"),
            {"o": world.org_b, "u": world.owner},
        )
    with tenant_session(user_id=world.owner_b, org_id=world.org_b, engine=app_engine) as s:
        assert policy_store.load_stored_policy(s, world.org_b).is_default
        assert s.execute(text("SELECT count(*) FROM ai_policies")).scalar_one() == 0
    with admin_engine.begin() as conn:
        conn.execute(text("DELETE FROM ai_policies WHERE org_id = :o"), {"o": world.org})
