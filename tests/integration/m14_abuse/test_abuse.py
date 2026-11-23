"""M14.07: per-user anomaly detection (flag once, throttle, block expiry) and canary documents."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import Engine, text
from tenancy_support import Api, Tenant

from app.core.db import tenant_session
from app.modules.firewall import abuse

CANARY_TEXT = "CANARY-SECRET-STRING-7f3a"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _sessions(engine: Engine) -> Any:
    return lambda *, user_id, org_id: tenant_session(user_id=user_id, org_id=org_id, engine=engine)


def _settings(api: Api, t: Tenant, **body: Any) -> None:
    res = api.put(f"/v1/orgs/{t.org_id}/security/abuse-settings", t.admin, body)
    assert res.status == 200, res.body


def _events(api: Api, t: Tenant, **q: str) -> list[dict[str, Any]]:
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    res = api.get(f"/v1/orgs/{t.org_id}/security/events?{qs}", t.admin)
    assert res.status == 200, res.body
    return res.body["events"]  # type: ignore[no-any-return]


def _doc(api: Api, t: Tenant, title: str = CANARY_TEXT) -> uuid.UUID:
    ws = api.create_workspace(t.org_id, t.admin)
    src, doc = uuid.uuid4(), uuid.uuid4()
    with api.admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sources (id, org_id, workspace_id, kind, name, created_by)"
                " VALUES (:s, :o, :w, 'upload', 'Uploads', :u)"
            ),
            {"s": src, "o": t.org_id, "w": ws, "u": t.admin.id},
        )
        conn.execute(
            text(
                "INSERT INTO documents (id, org_id, workspace_id, source_id, title, created_by)"
                " VALUES (:d, :o, :w, :s, :t, :u)"
            ),
            {"d": doc, "o": t.org_id, "w": ws, "s": src, "t": title, "u": t.admin.id},
        )
    return doc


def test_threshold_flags_once_per_window(api: Api, app_engine: Engine) -> None:
    t = api.tenant()
    _settings(api, t, action="flag", max_chat=2, window_seconds=60)
    sessions = _sessions(app_engine)
    for _ in range(6):  # never raises in flag mode
        abuse.guard(sessions, t.org_id, t.member.id, "chat")
    events = _events(api, t, kind="abuse_anomaly")
    assert len(events) == 1  # no event storm
    assert events[0]["user_id"] == str(t.member.id) and events[0]["counts"]["chat"] >= 3
    audits = api.scalar(
        "SELECT count(*) FROM audit_events WHERE org_id = :o AND event_type = :e",
        o=t.org_id,
        e="security.abuse.flagged",
    )
    assert audits == 1
    # distinct-document scraping counts too (separate user, own flag)
    _settings(api, t, action="flag", max_distinct_docs=3, window_seconds=60)
    for _ in range(5):
        abuse.guard(sessions, t.org_id, t.admin.id, None, document_ids=[uuid.uuid4()])
    docs = [e for e in _events(api, t) if e["user_id"] == str(t.admin.id)]
    assert len(docs) == 1 and docs[0]["counts"]["exceeded"] == ["distinct_documents"]


def test_throttle_returns_429_over_http_and_off_is_silent(api: Api, app_engine: Engine) -> None:
    t = api.tenant()
    _settings(api, t, action="throttle", max_chat=1, window_seconds=60)
    base = f"/v1/orgs/{t.org_id}/chat"
    tid = api.post(f"{base}/threads", t.member, {}).body["id"]
    ok = api.post(
        f"{base}/threads/{tid}/runs", t.member, {"content": "hi", "idempotency_key": "key-aaaaaaaa"}
    )
    assert ok.status in (200, 202), ok.body
    res = api.post(
        f"{base}/threads/{tid}/runs", t.member, {"content": "x", "idempotency_key": "key-bbbbbbbb"}
    )
    assert res.status == 429 and res.body["detail"] == "abuse_throttled"
    assert int(res.raw.headers["retry-after"]) >= 1
    other = api.tenant()  # another org is unaffected by this org's counters and settings
    assert abuse.guard(_sessions(app_engine), other.org_id, t.member.id, "chat") is None
    _settings(api, t, action="off", max_chat=1, window_seconds=60)
    for _ in range(3):
        abuse.guard(_sessions(app_engine), t.org_id, t.admin.id, "chat")


def test_block_expires(api: Api, app_engine: Engine) -> None:
    t = api.tenant()
    _settings(api, t, action="block", max_chat=1, window_seconds=10, block_seconds=10)
    clock = Clock()
    abuse.set_counters(abuse.Counters(None, clock=clock))
    sessions = _sessions(app_engine)
    abuse.guard(sessions, t.org_id, t.member.id, "chat")
    for _ in range(2):  # 2nd call crosses the threshold and blocks; 3rd is refused as blocked
        with pytest.raises(HTTPException) as ei:
            abuse.guard(sessions, t.org_id, t.member.id, "chat")
        assert ei.value.status_code == 429 and ei.value.detail == "abuse_throttled"
    abuse.guard(sessions, t.org_id, t.admin.id, "chat")  # other users are not blocked
    clock.now += 11
    abuse.guard(sessions, t.org_id, t.member.id, "chat")  # block and window both expired
    assert len(_events(api, t, kind="abuse_anomaly")) == 1


def test_canary_hit_alerts_without_text(api: Api, app_engine: Engine) -> None:
    t = api.tenant()
    canary, plain = _doc(api, t), _doc(api, t, "Plain")
    base = f"/v1/orgs/{t.org_id}/security/canaries"
    assert api.post(base, t.admin, {"document_id": str(canary)}).status == 201
    assert api.post(base, t.admin, {"document_id": str(uuid.uuid4())}).status == 404
    sessions = _sessions(app_engine)
    run = uuid.uuid4()
    assert abuse.canary_check(sessions, t.org_id, t.member.id, [plain]) == 0
    assert abuse.canary_check(sessions, t.org_id, t.member.id, [plain, canary], run_id=run) == 1
    assert abuse.canary_check(sessions, t.org_id, t.member.id, [canary]) == 0  # deduped
    (ev,) = _events(api, t, kind="canary_hit")
    assert ev["severity"] == "high" and ev["document_id"] == str(canary)
    assert ev["run_id"] == str(run) and ev["user_id"] == str(t.member.id)
    with api.admin_engine.connect() as conn:
        dump = json.dumps(
            [
                dict(r)
                for r in conn.execute(
                    text(
                        "SELECT s.*, a.metadata AS audit FROM security_events s"
                        " JOIN audit_events a ON a.target_id = s.id::text WHERE s.org_id = :o"
                    ),
                    {"o": t.org_id},
                ).mappings()
            ],
            default=str,
        )
    assert "canary" in dump.lower() and CANARY_TEXT not in dump
    with tenant_session(user_id=t.admin.id, org_id=t.org_id, engine=app_engine) as s:
        assert abuse.canary_document_ids(s, t.org_id) == {canary}
    # unmark: no more alerts
    assert api.delete(f"{base}/{canary}", t.admin).status == 204
    assert abuse.canary_check(sessions, t.org_id, t.admin.id, [canary]) == 0


def test_events_admin_only_and_cross_org(api: Api, app_engine: Engine) -> None:
    t = api.tenant()
    doc = _doc(api, t)
    api.post(f"/v1/orgs/{t.org_id}/security/canaries", t.admin, {"document_id": str(doc)})
    abuse.canary_check(_sessions(app_engine), t.org_id, t.member.id, [doc])
    base = f"/v1/orgs/{t.org_id}/security"
    (ev,) = _events(api, t)
    assert api.get(f"{base}/events", t.member).status == 403
    assert api.get(f"{base}/events", t.outsider).status == 404
    assert api.post(f"{base}/canaries", t.member, {"document_id": str(doc)}).status == 403
    assert api.put(f"{base}/abuse-settings", t.member, {"action": "block"}).status == 403
    other = api.tenant()  # an admin of another org cannot see or acknowledge it
    assert api.get(f"/v1/orgs/{other.org_id}/security/events", other.admin).body == {"events": []}
    foreign = f"/v1/orgs/{other.org_id}/security/events/{ev['id']}/acknowledge"
    assert api.post(foreign, other.admin, {}).status == 404
    assert (
        api.post(
            f"/v1/orgs/{other.org_id}/security/canaries", other.admin, {"document_id": str(doc)}
        ).status
        == 404
    )
    ack = api.post(f"{base}/events/{ev['id']}/acknowledge", t.admin, {})
    assert ack.status == 200 and ack.body["acknowledged_by"] == str(t.admin.id)
    assert _events(api, t, acknowledged="false") == []
