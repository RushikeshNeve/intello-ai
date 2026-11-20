"""M07.01: memory lifecycle, approval gate, evidence validation, invalidation, expiry, privacy, and
the chat integration (an unapproved or stale memory never reaches the prompt)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from catalog_support import World
from chat_support import ask_ok, new_thread, run_events
from ret_support import Published, Rig
from sqlalchemy import Engine, text

from app.core.config import Settings
from app.modules.ai.chat import ChatDeps
from app.modules.ai.models.fake import FakeChatModel
from app.modules.ai.models.settings import ChatSettings
from app.modules.ai.models.types import ChatMessage
from app.modules.memory import invalidation
from app.workers.chat_tasks import run_chat

HERON = "Herons are wading birds that feed on fish and frogs in shallow water."
OTTER = "Otters build their dens near rivers and eat crayfish at dusk."
STATEMENT = "Otters are most active at dusk and then eat crayfish."


class Env:
    def __init__(self, world: World, rig: Rig, worker: Engine, base: Settings) -> None:
        self.world, self.rig, self.worker, self.base = world, rig, worker, base

    @property
    def base_path(self) -> str:
        return f"/v1/orgs/{self.world.org_id}/memory"

    def publish(self, text_: str, *, indexed: bool = True, versions: int = 1) -> Published:
        doc = self.rig.seed_world(self.world, title="Doc", versions=versions)
        return self.rig.publish(self.world.org_id, self.world.ws, doc, [text_], flag=indexed)

    def body(self, pub: Published, **extra: Any) -> dict[str, Any]:
        return {
            "statement": STATEMENT,
            "kind": "fact",
            "evidence": [
                {
                    "chunk_id": str(pub.chunk_ids[0]),
                    "document_id": str(pub.document_id),
                    "version_id": str(pub.version_id),
                }
            ],
            **extra,
        }

    def suggest(self, user: Any, pub: Published, **extra: Any) -> dict[str, Any]:
        res = self.world.api.post(f"{self.base_path}/suggestions", user, self.body(pub, **extra))
        assert res.status == 201, res.body
        return res.body  # type: ignore[no-any-return]

    def get(self, user: Any, memory_id: str) -> Any:
        return self.world.api.get(f"{self.base_path}/{memory_id}", user)

    def sql(self, sql: str, **params: Any) -> None:
        with self.rig.admin.begin() as conn:
            conn.execute(text(sql), params)

    def delete_document(self, pub: Published) -> None:
        self.sql(
            "UPDATE documents SET deleted_at = now(), deleted_by = created_by WHERE id = :d",
            d=pub.document_id,
        )

    def chat(
        self, user: Any, question: str, model: FakeChatModel
    ) -> tuple[uuid.UUID, dict[str, Any]]:
        w = self.world
        thread = new_thread(w.api, w.org_id, user)
        run = ask_ok(w.api, w.org_id, thread, user, question)
        deps = ChatDeps(
            repo=self.rig.repo,
            embedder=self.rig.embedder,
            chat_settings=ChatSettings(),
            model_factory=lambda _ps, _org, _cs: model,
        )
        outcome = run_chat(run, engine=self.worker, settings=self.base, deps_provider=lambda: deps)
        assert outcome == "completed", outcome
        return run, run_events(w.engine, run)[-1]


@pytest.fixture
def env(world: World, rig: Rig, worker_engine: Engine, base_settings: Settings) -> Env:
    return Env(world, rig, worker_engine, base_settings)


def _prompt(messages: Sequence[ChatMessage]) -> str:
    return "\n".join(m["content"] for m in messages)


def _seen_by(model: FakeChatModel) -> str:
    """Everything the model was shown ('' when it was never called)."""
    return "\n".join(_prompt(c.messages) for c in model.calls)


def _answering_model() -> FakeChatModel:
    """Answers from the memory block when the prompt has one, else from the first passage."""

    def reply(messages: Sequence[ChatMessage]) -> str:
        prompt = _prompt(messages)
        for line in prompt.splitlines():
            if "| Memory (approved) |" in line:
                n = line.split("<<<EVIDENCE ")[1].split(" ")[0]
                return f"Otters eat crayfish at dusk [{n}]."
        return "I don't know based on the available documents."

    return FakeChatModel(reply_fn=reply)


# ---------------------------------------------------------------------------------- lifecycle


def test_lifecycle_approval_gate_and_privacy(env: Env) -> None:
    w, api = env.world, env.world.api
    pub = env.publish(OTTER)
    memory = env.suggest(w.viewer, pub)
    assert memory["state"] == "suggested"
    assert memory["visibility"] == "private"
    assert memory["evidence"][0]["chunk_id"] == str(pub.chunk_ids[0])

    detail = env.get(w.viewer, memory["id"]).body
    assert detail["evidence"][0]["evidence_status"] == "ok"
    listed = api.get(f"{env.base_path}?state=suggested&limit=5", w.viewer).body
    assert memory["id"] in [m["id"] for m in listed["items"]]

    # private to its owner: another org member and another org see a 404 on every verb
    for other in (w.editor, w.t.owner, w.foreign.owner):
        assert env.get(other, memory["id"]).status == 404
        assert api.post(f"{env.base_path}/{memory['id']}/approve", other, None).status == 404
        assert api.delete(f"{env.base_path}/{memory['id']}", other).status == 404
    assert api.get(env.base_path, w.editor).body["items"] == []

    # approval is recorded; a second decision conflicts
    approved = api.post(f"{env.base_path}/{memory['id']}/approve", w.viewer, None)
    assert approved.status == 200
    assert approved.body["state"] == "approved"
    assert approved.body["approved_by"] == str(w.viewer.id)
    assert approved.body["approved_at"]
    assert api.post(f"{env.base_path}/{memory['id']}/reject", w.viewer, None).status == 409

    # audit: ids and counts only
    with env.rig.admin.connect() as conn:
        rows = conn.execute(
            text("SELECT event_type, metadata FROM audit_events WHERE target_id = :t"),
            {"t": memory["id"]},
        ).all()
    assert {r.event_type for r in rows} == {"memory.suggested", "memory.approved"}
    assert all(STATEMENT not in str(r.metadata) for r in rows)

    # owner-only delete
    assert api.delete(f"{env.base_path}/{memory['id']}", w.viewer).status == 204
    assert env.get(w.viewer, memory["id"]).status == 404


def test_evidence_the_user_cannot_read_is_rejected(env: Env) -> None:
    w, api = env.world, env.world.api
    pub = env.publish(OTTER)
    # an org member without any workspace role has nothing readable at all: concealed 404
    assert api.post(f"{env.base_path}/suggestions", w.plain, env.body(pub)).status == 404
    # a document in a workspace the user cannot read: refused like a missing chunk
    ws2 = api.create_workspace(w.org_id, w.t.owner)
    other = env.rig.publish(
        w.org_id, ws2, env.rig.seed(w.org_id, ws2, w.t.owner, title="Hidden"), [OTTER]
    )
    res = api.post(f"{env.base_path}/suggestions", w.viewer, env.body(other))
    assert res.status == 422
    assert res.body["detail"] == "invalid_evidence"
    # a chunk that does not exist, a fact without evidence, a past expiry
    bad = env.body(pub)
    bad["evidence"][0]["chunk_id"] = str(uuid.uuid4())
    assert (
        api.post(f"{env.base_path}/suggestions", w.viewer, bad).body["detail"] == "invalid_evidence"
    )
    no_ev = {**env.body(pub), "evidence": []}
    assert (
        api.post(f"{env.base_path}/suggestions", w.viewer, no_ev).body["detail"]
        == "evidence_required"
    )
    past = env.body(pub, expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat())
    assert (
        api.post(f"{env.base_path}/suggestions", w.viewer, past).body["detail"] == "invalid_expiry"
    )
    # another org: not a member at all
    assert api.post(f"{env.base_path}/suggestions", w.foreign.owner, env.body(pub)).status == 404


def test_workspace_visible_memory_needs_a_workspace_admin(env: Env) -> None:
    w, api = env.world, env.world.api
    pub = env.publish(OTTER)
    memory = env.suggest(w.viewer, pub, visibility="workspace", workspace_id=str(w.ws))
    # its owner is only a viewer: they cannot approve it; the workspace admin can
    assert api.post(f"{env.base_path}/{memory['id']}/approve", w.viewer, None).status == 403
    assert env.get(w.editor, memory["id"]).status == 404  # not approved yet: admins only
    done = api.post(f"{env.base_path}/{memory['id']}/approve", w.ws_admin, None)
    assert done.status == 200
    assert done.body["approved_by"] == str(w.ws_admin.id)
    assert env.get(w.editor, memory["id"]).body["state"] == "approved"  # workspace member
    assert env.get(w.plain, memory["id"]).status == 404  # not a workspace member
    assert env.get(w.foreign.owner, memory["id"]).status == 404  # another org


# ------------------------------------------------------------------------- chat integration


def test_chat_uses_only_approved_valid_memory(env: Env) -> None:
    w, api = env.world, env.world.api
    env.publish(HERON)  # the indexed knowledge; the memory's evidence is NOT in the index
    pub = env.publish(OTTER, indexed=False)
    question = "What do otters eat at dusk?"
    memory = env.suggest(w.viewer, pub)

    # 1. a suggestion is not a fact: it never reaches the prompt
    model = _answering_model()
    env.chat(w.viewer, question, model)
    assert STATEMENT not in _seen_by(model)

    # 2. approved: a separate "Memory (approved)" passage, cited with its evidence chunk
    assert api.post(f"{env.base_path}/{memory['id']}/approve", w.viewer, None).status == 200
    model = _answering_model()
    run, final = env.chat(w.viewer, question, model)
    assert STATEMENT in _seen_by(model)
    assert "Memory (approved)" in _seen_by(model)
    assert "crayfish" in final["data"]["answer_text"]
    msg = api.get(f"/v1/orgs/{w.org_id}/chat/runs/{run}", w.viewer).body["message"]
    assert [c["chunk_id"] for c in msg["citations"]] == [str(pub.chunk_ids[0])]

    # 3. stale evidence (document deleted): skipped, no error
    env.delete_document(pub)
    model = _answering_model()
    env.chat(w.viewer, question, model)
    assert STATEMENT not in _seen_by(model)


# ------------------------------------------------------------------- invalidation and expiry


def test_evidence_changes_invalidate_and_expiry_expires(env: Env) -> None:
    w, api = env.world, env.world.api

    # document deleted: lazily at read time ...
    pub = env.publish(OTTER)
    m1 = env.suggest(w.viewer, pub)
    api.post(f"{env.base_path}/{m1['id']}/approve", w.viewer, None)
    env.delete_document(pub)
    read = env.get(w.viewer, m1["id"]).body
    assert read["state"] == "invalidated"
    assert read["invalidated_reason"] == "evidence_deleted"
    assert read["evidence"][0]["evidence_status"] == "stale"

    # ... by the document.deleted worker hook ...
    pub2 = env.publish(OTTER)
    m2 = env.suggest(w.viewer, pub2)
    env.delete_document(pub2)
    assert invalidation.invalidate_document(env.worker, w.org_id, pub2.document_id) == 1
    assert env.get(w.viewer, m2["id"]).body["invalidated_reason"] == "evidence_deleted"

    # ... and by the sweep: superseded evidence (a newer version is published) and expiry
    doc = env.rig.seed_world(w, title="Versioned", versions=2)
    v1 = env.rig.publish(w.org_id, w.ws, doc, [OTTER], version=1)
    m3 = env.suggest(
        w.viewer,
        v1,
    )
    env.rig.publish(w.org_id, w.ws, doc, [OTTER + " Updated."], version=2)
    pub4 = env.publish(OTTER)
    m4 = env.suggest(
        w.viewer, pub4, expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat()
    )
    api.post(f"{env.base_path}/{m4['id']}/approve", w.viewer, None)
    env.sql(
        "UPDATE memories SET expires_at = now() - interval '1 minute' WHERE id = :i", i=m4["id"]
    )
    result = invalidation.sweep(env.worker)
    assert result.expired >= 1
    assert result.invalidated >= 1
    assert env.get(w.viewer, m3["id"]).body["invalidated_reason"] == "evidence_superseded"
    assert env.get(w.viewer, m4["id"]).body["state"] == "expired"
