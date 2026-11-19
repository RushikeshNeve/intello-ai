"""Optional smoke against a REAL local model (LM Studio, OpenAI-compatible ``/v1``): skipped unless
the server answers. Nothing else in this suite needs a model; this only proves the production
wiring (gateway ``get_chat_model`` -> ``LocalChatModel`` -> streamed deltas -> verification).

``CHAT_SMOKE_BASE_URL`` (default ``http://host.docker.internal:1234/v1``) and ``CHAT_MODEL``
select it.
"""

from __future__ import annotations

import os

import httpx
import pytest
from catalog_support import World
from chat_support import HERON, HERON_Q, ask_ok, event_types, new_thread, run_events, run_row
from ret_support import Rig
from sqlalchemy import Engine

from app.core.config import Settings
from app.modules.ai.chat import ChatDeps, default_model_factory
from app.modules.ai.models.settings import ChatSettings
from app.workers.chat_tasks import run_chat

BASE_URL = os.environ.get("CHAT_SMOKE_BASE_URL", "http://host.docker.internal:1234/v1")


def _reachable() -> bool:
    try:
        return httpx.get(f"{BASE_URL}/models", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(not _reachable(), reason="no local LM Studio server answering at the URL")
def test_real_local_model_streams_a_grounded_answer(
    world: World, rig: Rig, worker_engine: Engine, base_settings: Settings
) -> None:
    doc = rig.seed_world(world, title="Birds")
    rig.publish(world.org_id, world.ws, doc, [HERON])
    thread = new_thread(world.api, world.org_id, world.viewer)
    run = ask_ok(world.api, world.org_id, thread, world.viewer, HERON_Q)
    deps = ChatDeps(
        repo=rig.repo,
        embedder=rig.embedder,
        chat_settings=ChatSettings(AI_BASE_URL=BASE_URL, CHAT_GENERATION_TIMEOUT_SECONDS=120),
        model_factory=default_model_factory,
    )
    outcome = run_chat(
        run, engine=worker_engine, settings=base_settings, deps_provider=lambda: deps
    )
    assert outcome in ("completed", "failed")
    row = run_row(world.engine, run)
    if outcome == "failed":  # a model that is not loaded is an honest, safe failure
        assert row["error_code"] == "model_unavailable"
        return
    events = run_events(world.engine, run)
    assert event_types(world.engine, run)[-1] == "completed"
    assert events[-2]["data"]["status"] in ("grounded", "partial", "abstained")
    assert events[-1]["data"]["answer_text"]
