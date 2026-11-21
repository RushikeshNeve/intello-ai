"""Budgets and quotas (M11.08): CRUD + consumption, block/warn, scopes, period reset, burst."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine
from tenancy_support import Api

from app.core.db import tenant_session
from app.modules.ai.models.fake import FakeChatModel
from app.modules.usage.limits import QuotaExceeded, QuotaGuard, check_quota
from app.modules.usage.meter import MeteredChatModel
from app.modules.usage.recorder import UsageContext, UsageRecorder

MSG: Any = [{"role": "user", "content": "x" * 40}]


def _sessions(engine: Engine) -> Any:
    return lambda org: tenant_session(user_id=None, org_id=org, engine=engine)


def _record(
    engine: Engine, org: uuid.UUID, user: uuid.UUID, kind: str = "chat", n: int = 1
) -> None:
    rec = UsageRecorder(_sessions(engine), prices_json="")
    for _ in range(n):
        rec.record(
            UsageContext(org, kind, user_id=user),
            provider="l",
            model="m",
            tokens_in=10,
            tokens_out=5,
        )


def _check(engine: Engine, org: uuid.UUID, user: uuid.UUID, kind: str = "chat", **kw: Any) -> Any:
    with tenant_session(user_id=None, org_id=org, engine=engine) as s:
        return check_quota(s, org_id=org, user_id=user, kind=kind, **kw)


def _limit(api: Api, t: Any, **body: Any) -> dict[str, Any]:
    res = api.post(f"/v1/orgs/{t.org_id}/usage/limits", t.admin, {"max_requests": 2, **body})
    assert res.status == 201, res.body
    return res.body  # type: ignore[no-any-return]


def test_block_warn_scopes_and_period_reset(api: Api, app_engine: Engine) -> None:
    t = api.tenant()
    _limit(api, t, kind="chat", scope="org", action="block")
    _record(app_engine, t.org_id, t.member.id, n=1)
    assert _check(app_engine, t.org_id, t.admin.id).warnings == []  # 1 < 2: allowed
    _record(app_engine, t.org_id, t.admin.id, n=1)
    with pytest.raises(QuotaExceeded) as ei:  # org total (both users) reached 2
        _check(app_engine, t.org_id, t.member.id)
    assert ei.value.retry_after_seconds() >= 1
    _check(app_engine, t.org_id, t.member.id, kind="embed")  # other kind is not limited
    # period reset: next month has no consumption
    nxt = datetime(2099, 1, 5, tzinfo=UTC)
    assert _check(app_engine, t.org_id, t.member.id, now=nxt).warnings == []

    # consumption shown by GET
    got = api.get(f"/v1/orgs/{t.org_id}/usage/limits", t.admin).body["limits"][0]
    assert (got["consumed_requests"], got["consumed_tokens"], got["state"]) == (2, 30, "blocked")

    # per-user default scope: counts each user separately
    t2 = api.tenant()
    _limit(api, t2, kind="all", scope="user", action="block")
    _record(app_engine, t2.org_id, t2.member.id, n=2)
    with pytest.raises(QuotaExceeded):
        _check(app_engine, t2.org_id, t2.member.id)
    assert _check(app_engine, t2.org_id, t2.admin.id).warnings == []

    # warn mode never blocks
    t3 = api.tenant()
    _limit(api, t3, kind="all", scope="org", action="warn", max_requests=1)
    _record(app_engine, t3.org_id, t3.member.id, n=3)
    assert _check(app_engine, t3.org_id, t3.member.id).warnings == ["all/month"]

    # burst limit
    with pytest.raises(QuotaExceeded):
        _check(app_engine, t3.org_id, t3.member.id, burst_per_minute=3)


def test_guard_blocks_before_the_model_call_and_fails_open(api: Api, app_engine: Engine) -> None:
    t = api.tenant()
    _limit(api, t, kind="chat", max_requests=0)
    calls: list[int] = []
    inner = FakeChatModel(reply_fn=lambda m: calls.append(1) or "ok")
    guard = QuotaGuard(_sessions(app_engine), org_id=t.org_id, user_id=t.member.id)
    model = MeteredChatModel(
        inner, UsageRecorder(_sessions(app_engine)), UsageContext(t.org_id, "chat"), guard
    )
    with pytest.raises(QuotaExceeded):
        model.complete(MSG)
    assert calls == []
    broken = QuotaGuard(_sessions(app_engine), org_id=uuid.uuid4(), user_id=None)
    broken.check("chat")  # no limits for an unknown org: allowed


def test_crud_admin_only_and_cross_tenant(api: Api) -> None:
    t = api.tenant()
    base = f"/v1/orgs/{t.org_id}/usage/limits"
    made = _limit(api, t)
    assert api.get(base, t.member).status == 403
    assert api.get(base, t.outsider).status == 404
    assert api.post(base, t.outsider, {"max_requests": 1}).status == 404
    assert api.post(base, t.admin, {"kind": "chat"}).status == 422  # no maximum
    put = api.put(f"{base}/{made['id']}", t.admin, {"max_tokens": 9, "action": "warn"})
    assert put.status == 200 and put.body["action"] == "warn"
    other = api.tenant()
    gone = api.delete(f"/v1/orgs/{other.org_id}/usage/limits/{made['id']}", other.admin)
    assert gone.status == 404
    assert api.delete(f"{base}/{made['id']}", t.admin).status == 204
    assert api.get(base, t.admin).body["limits"] == []
