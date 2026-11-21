"""Usage metering (M11.01): recorder through RLS, the summary endpoint, org-delete cascade."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Engine, text
from tenancy_support import Api

from app.core.db import tenant_session
from app.modules.ai.models.fake import FakeChatModel
from app.modules.usage.meter import MeteredChatModel
from app.modules.usage.recorder import UsageContext, UsageRecorder, estimate_cost_micros

MSG: Any = [{"role": "user", "content": "x" * 40}]


class _Sink(UsageRecorder):
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, ctx: UsageContext, **kw: Any) -> None:  # type: ignore[override]
        self.events.append({"kind": ctx.kind, **kw})


def _recorder(engine: Engine, prices: str = "") -> UsageRecorder:
    return UsageRecorder(
        lambda org: tenant_session(user_id=None, org_id=org, engine=engine), prices_json=prices
    )


def test_one_event_per_call_and_failures_never_leak(app_engine: Engine) -> None:
    sink = _Sink()
    ctx = UsageContext(uuid.uuid4(), "chat")
    model = MeteredChatModel(FakeChatModel(["hello world"]), sink, ctx)
    assert model.complete(MSG) == "hello world"
    assert len(sink.events) == 1
    assert (sink.events[0]["tokens_in"], sink.events[0]["tokens_out"]) == (10, 3)

    it = model.stream(MSG)
    next(it)
    it.close()  # cancelled mid-stream: still exactly one event
    assert len(sink.events) == 2

    # A recorder that cannot write (no such org) must not fail the model call.
    broken = MeteredChatModel(
        FakeChatModel(["ok"]), _recorder(app_engine), UsageContext(uuid.uuid4(), "chat")
    )
    assert broken.complete(MSG) == "ok"


def test_price_table_defaults_to_zero() -> None:
    assert estimate_cost_micros("{}", "local", "m", 1000, 1000, 0) == 0
    table = '{"openai/*": {"in": 1000000, "out": 2000000}}'
    assert estimate_cost_micros(table, "openai", "gpt", 10, 5, 0) == 20
    assert estimate_cost_micros("not json", "openai", "gpt", 10, 5, 0) == 0


def test_summary_scoping_cross_tenant_and_cascade(api: Api, app_engine: Engine) -> None:
    t = api.tenant()
    rec = _recorder(app_engine, '{"local/*": {"in": 1000000}}')
    for user, kind, model in (
        (t.admin, "chat", "m-a"),
        (t.member, "embed", "m-b"),
        (t.member, "chat", "m-b"),
    ):
        rec.record(
            UsageContext(t.org_id, kind, user_id=user.id),
            provider="local",
            model=model,
            tokens_in=10,
            tokens_out=2,
            latency_ms=5,
        )
    # Another org's event must never show up.
    rec.record(
        UsageContext(t.other_org_id, "chat"),
        provider="local",
        model="x",
        tokens_in=99,
        tokens_out=0,
    )
    base = f"/v1/orgs/{t.org_id}/usage"

    admin = api.get(f"{base}?group_by=kind", t.admin)
    assert admin.status == 200, admin.body
    assert admin.body["scope"] == "org"
    assert {r["key"]: r["events"] for r in admin.body["rows"]} == {"chat": 2, "embed": 1}
    assert admin.body["totals"]["tokens_in"] == 30
    assert admin.body["totals"]["est_cost_micros"] == 30

    member = api.get(f"{base}?group_by=model", t.member)
    assert member.body["scope"] == "self"
    assert member.body["totals"]["events"] == 2
    assert {r["key"] for r in member.body["rows"]} == {"local/m-b"}

    assert api.get(base, t.outsider).status == 404
    assert (
        api.get(f"{base}?from=2030-01-01T00:00:00Z&to=2029-01-01T00:00:00Z", t.admin).status == 422
    )

    with api.admin_engine.begin() as conn:
        conn.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": t.org_id})
        left = conn.execute(
            text("SELECT count(*) FROM usage_events WHERE org_id = :o"), {"o": t.org_id}
        ).scalar_one()
    assert left == 0


def test_unknown_group_by_is_rejected(api: Api) -> None:
    t = api.tenant()
    assert api.get(f"/v1/orgs/{t.org_id}/usage?group_by=hour", t.admin).status == 422
