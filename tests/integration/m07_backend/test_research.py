"""M07.02: research sessions end to end on a real database as the real worker role (in-process):
API -> outbox -> claim -> plan -> search -> synthesize -> sourced report -> markdown export, plus
limits, resume after a worker crash (no repeated model call, no duplicate step), cancel, withheld
unsupported statements, privacy and the dispatcher hooks."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from collections.abc import Sequence
from typing import Any

import pytest
from catalog_support import World
from ret_support import Published, Rig
from sqlalchemy import Engine, text

from app.core.config import Settings
from app.modules.ai.chat import ChatDeps
from app.modules.ai.models.fake import FakeChatModel
from app.modules.ai.models.settings import ChatSettings
from app.modules.ai.models.types import ChatMessage
from app.modules.research import repo as research_repo
from app.workers.dispatcher import ROUTES
from app.workers.research_tasks import run_research
from app.workers.worker_db import system_connection

HERON = "Herons are wading birds that feed on fish and frogs in shallow water."
GOOD = "Herons feed on fish and frogs in shallow water [1]."
BAD = "Herons are made entirely of cheese and live on the moon [1]."
QUESTION = "What do herons eat and where? <script>alert(1)</script>"


class Crash(BaseException):
    """Simulates the worker process dying mid-run (not an ``Exception``: nothing catches it)."""


class Env:
    def __init__(self, world: World, rig: Rig, worker: Engine, base: Settings) -> None:
        self.world, self.rig, self.worker, self.base = world, rig, worker, base

    @property
    def path(self) -> str:
        return f"/v1/orgs/{self.world.org_id}/research"

    def start(self, user: Any = None, **body: Any) -> uuid.UUID:
        res = self.world.api.post(
            self.path, user or self.world.viewer, {"question": QUESTION, **body}
        )
        assert res.status == 202, res.body
        assert res.body["state"] == "planned"
        return uuid.UUID(res.body["id"])

    def detail(self, session: uuid.UUID, user: Any = None) -> dict[str, Any]:
        res = self.world.api.get(f"{self.path}/{session}", user or self.world.viewer)
        assert res.status == 200, res.body
        return res.body  # type: ignore[no-any-return]

    def run(self, session: uuid.UUID, model: FakeChatModel, **settings: Any) -> Any:
        deps = ChatDeps(
            repo=self.rig.repo,
            embedder=self.rig.embedder,
            chat_settings=ChatSettings(),
            model_factory=lambda _ps, _org, _cs: model,
        )
        return run_research(
            session,
            engine=self.worker,
            settings=self.base.model_copy(update=settings),
            deps_provider=lambda: deps,
        )

    def publish(self, body: str = HERON) -> Published:
        doc = self.rig.seed_world(self.world, title="Birds")
        return self.rig.publish(self.world.org_id, self.world.ws, doc, [body])

    def count(self, sql: str, **params: Any) -> int:
        with self.rig.admin.connect() as conn:
            return int(conn.execute(text(sql), params).scalar_one())


@pytest.fixture
def env(world: World, rig: Rig, worker_engine: Engine, base_settings: Settings) -> Env:
    return Env(world, rig, worker_engine, base_settings)


def plan_json(*queries: str) -> str:
    return json.dumps({"subqueries": list(queries), "ignored": "field"})


def scripted(plan: str, synth: str = GOOD) -> FakeChatModel:
    def reply(messages: Sequence[ChatMessage]) -> str:
        return plan if messages[0]["content"].startswith("You break") else synth

    return FakeChatModel(reply_fn=reply)


# -------------------------------------------------------------------------------- full run


def test_full_run_report_is_sourced_and_exports_without_html(env: Env) -> None:
    w = env.world
    pub = env.publish()
    session = env.start()
    assert (
        env.count(
            "SELECT count(*) FROM outbox_events WHERE aggregate_id = :s AND event_type"
            " = 'research.session.queued'",
            s=session,
        )
        == 1
    )
    model = scripted(plan_json("what do herons feed on", "where do herons feed in water"))
    assert env.run(session, model) == "completed"

    done = env.detail(session)
    assert done["state"] == "completed"
    assert done["plan"]["subqueries"] == [
        "what do herons feed on",
        "where do herons feed in water",
    ]
    assert [s["kind"] for s in done["steps"]] == [
        "plan", "search", "synthesize", "search", "synthesize", "report",
    ]  # fmt: skip
    assert {s["state"] for s in done["steps"]} == {"done"}
    assert done["usage"]["model_calls"] == 3
    assert len(model.calls) == 3
    assert done["usage"]["evidence_chunks"] >= 2
    sections = done["report"]["sections"]
    assert len(sections) == 2
    statements = [st for sec in sections for st in sec["statements"]]
    assert statements, done["report"]
    for st in statements:  # every statement carries a resolvable citation to the original chunk
        assert st["citations"]
        for c in st["citations"]:
            assert c["chunk_id"] == str(pub.chunk_ids[0])
            assert c["title"] == "Birds"
    assert done["report"]["verification"]["status"] in ("grounded", "partial")

    export = w.api.client.get(
        f"{env.path}/{session}/export?format=md",
        headers={"Authorization": f"Bearer {w.viewer.token}"},
    )  # markdown, not JSON: bypass the JSON-decoding helper
    assert export.status_code == 200
    assert "text/markdown" in export.headers["content-type"]
    md = export.text
    assert "[^1]" in md
    assert "Birds" in md
    assert "<script" not in md
    assert "<" not in md
    assert w.api.get(f"{env.path}/{session}/export?format=html", w.viewer).status == 422

    # the report is re-resolved at read time: a deleted document drops its statements
    with env.rig.admin.begin() as conn:
        conn.execute(
            text("UPDATE documents SET deleted_at = now(), deleted_by = created_by WHERE id = :d"),
            {"d": pub.document_id},
        )
    after = env.detail(session)["report"]
    assert all(not sec["statements"] for sec in after["sections"])
    assert after["verification"]["withheld_at_read"] == len(statements)


def test_unsupported_statements_are_withheld(env: Env) -> None:
    env.publish()
    session = env.start()
    model = scripted(plan_json("what do herons feed on"), synth=f"{GOOD} {BAD}")
    assert env.run(session, model) == "completed"
    report = env.detail(session)["report"]
    kept = [st["text"] for sec in report["sections"] for st in sec["statements"]]
    assert kept
    assert all("cheese" not in t for t in kept)
    assert report["verification"]["withheld_count"] >= 1
    assert all(st["citations"] for sec in report["sections"] for st in sec["statements"])


# ----------------------------------------------------------------------------------- limits


def test_plan_limits_are_enforced(env: Env) -> None:
    w = env.world
    env.publish()
    # refused before any work starts: a limit can only lower the ceiling
    too_high = w.api.post(env.path, w.viewer, {"question": "q?", "limits": {"max_subqueries": 99}})
    assert too_high.status == 422
    assert too_high.body["detail"] == "limit_exceeded"
    # a workspace the caller cannot read does not exist for them
    foreign = w.api.post(
        env.path, w.viewer, {"question": "q?", "workspace_ids": [str(w.foreign_ws)]}
    )
    assert foreign.status == 404

    session = env.start(limits={"max_subqueries": 2, "max_evidence": 1})
    model = scripted(plan_json("a heron", "b heron", "c heron", "d heron", "e heron"))
    assert env.run(session, model) == "completed"
    done = env.detail(session)
    assert len(done["plan"]["subqueries"]) == 2  # 5 proposed, 2 allowed
    assert len(model.calls) == 2  # the plan + one synthesis; the 2nd sub-query has no budget
    assert done["usage"]["evidence_chunks"] <= 1  # the evidence budget caps retrieval

    # the model-call ceiling bounds the plan too: 2 calls = the plan and one synthesis
    tight = env.start(limits={"max_model_calls": 2})
    model = scripted(plan_json("heron one", "heron two", "heron three"))
    assert env.run(tight, model) == "completed"
    assert len(env.detail(tight)["plan"]["subqueries"]) == 1
    assert len(model.calls) == 2

    # a plan that is not JSON degrades to the question itself
    plain = env.start()
    assert env.run(plain, scripted("not json at all")) == "completed"
    assert len(env.detail(plain)["plan"]["subqueries"]) == 1


# ---------------------------------------------------------------- recovery and cancellation


def test_resume_after_a_worker_crash_repeats_no_step_and_no_model_call(env: Env) -> None:
    env.publish()
    session = env.start()
    calls: list[str] = []

    def reply(messages: Sequence[ChatMessage]) -> str:
        user = messages[-1]["content"]
        if messages[0]["content"].startswith("You break"):
            calls.append("plan")
            return plan_json("what do herons feed on", "where do herons wade")
        calls.append(user)
        if len(calls) == 3:  # the second synthesis: the process dies
            raise Crash
        return GOOD

    with pytest.raises(Crash):
        env.run(session, FakeChatModel(reply_fn=reply))
    crashed = env.detail(session)
    assert crashed["state"] == "running"
    assert [s["state"] for s in crashed["steps"]] == [
        "done", "done", "done", "done", "running", "pending",
    ]  # fmt: skip

    # the dispatcher's reaper (lease expired) puts it back with a fresh outbox row
    with env.rig.admin.begin() as conn:
        conn.execute(
            text(
                "UPDATE research_sessions SET lease_expires_at = now() - interval '1 second'"
                " WHERE id = :s"
            ),
            {"s": session},
        )
    with system_connection(env.worker) as conn:
        reaped = research_repo.reap_expired(conn, 3)
    assert [(r.session_id, r.outcome) for r in reaped] == [(session, "requeued")]
    assert (
        env.count(
            "SELECT count(*) FROM outbox_events WHERE aggregate_id = :s AND event_type"
            " = 'research.session.queued'",
            s=session,
        )
        == 2
    )

    resumed = scripted(plan_json("never used"))
    assert env.run(session, resumed) == "completed"
    assert len(resumed.calls) == 1  # only the interrupted synthesis: plan and synthesis 1 kept
    done = env.detail(session)
    assert done["state"] == "completed"
    assert len(done["steps"]) == 6
    assert done["plan"]["subqueries"][0] == "what do herons feed on"
    assert done["usage"]["model_calls"] == 3
    assert env.count("SELECT count(*) FROM research_steps WHERE session_id = :s", s=session) == 6
    assert env.count("SELECT attempts FROM research_sessions WHERE id = :s", s=session) == 2


def test_cancel_before_and_during_a_run(env: Env) -> None:
    w = env.world
    env.publish()
    first = env.start()
    res = w.api.post(f"{env.path}/{first}/cancel", w.viewer, None)
    assert res.status == 202
    assert res.body["state"] == "cancelled"
    assert env.run(first, scripted(plan_json("x heron"))) == "skipped"  # nothing to claim
    assert w.api.post(f"{env.path}/{first}/cancel", w.viewer, None).body["state"] == "cancelled"

    second = env.start()

    def cancel_midway(messages: Sequence[ChatMessage]) -> str:
        w.api.post(f"{env.path}/{second}/cancel", w.viewer, None)
        return plan_json("what do herons feed on")

    assert env.run(second, FakeChatModel(reply_fn=cancel_midway)) == "fence_lost"
    assert env.detail(second)["state"] == "cancelled"
    assert env.count("SELECT count(*) FROM research_reports WHERE session_id = :s", s=second) == 0


def test_a_failure_ends_the_session_with_a_safe_code(env: Env) -> None:
    env.publish()
    session = env.start()

    def boom(messages: Sequence[ChatMessage]) -> str:
        raise RuntimeError("SECRET-INTERNAL-DETAIL")

    assert env.run(session, FakeChatModel(reply_fn=boom)) == "failed"
    done = env.detail(session)
    assert done["state"] == "failed"
    assert done["error_code"] == "internal_error"
    assert "SECRET" not in json.dumps(done)
    assert done["steps"][0]["state"] == "failed"


# ------------------------------------------------------------------------------------ privacy


def test_sessions_are_private_to_their_creator(env: Env) -> None:
    w, api = env.world, env.world.api
    session = env.start()
    for other in (w.editor, w.t.owner, w.foreign.owner):
        assert api.get(f"{env.path}/{session}", other).status == 404
        assert api.post(f"{env.path}/{session}/cancel", other, None).status == 404
        assert api.get(f"{env.path}/{session}/export", other).status == 404
    assert api.get(env.path, w.editor).body["items"] == []
    mine = api.get(env.path, w.viewer).body["items"]
    assert session in [uuid.UUID(i["id"]) for i in mine]
    assert api.get(f"{env.path}/{session}/export", w.viewer).status == 409  # no report yet


def test_the_dispatcher_routes_the_event() -> None:
    sid = uuid.uuid4()
    row = SimpleNamespace(org_id=uuid.uuid4(), aggregate_id=sid, payload={"session_id": str(sid)})
    delivery = ROUTES["research.session.queued"](row)  # type: ignore[arg-type]
    assert (delivery.task, delivery.queue, delivery.args) == ("research.run", "research", (str(sid),))
