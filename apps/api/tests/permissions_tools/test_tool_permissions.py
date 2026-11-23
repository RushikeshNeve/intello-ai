"""M14.06: default-deny tool registry; a registered tool is unreachable unless enabled per run."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict

from app.modules.ai.models.types import ModelSpec
from app.modules.ai.models.wire import OpenAICompatibleChat
from app.modules.permissions_tools import REGISTRY, RunContext, ToolRegistry, ToolUser


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str


def _setup(confirm: bool = False) -> tuple[ToolRegistry, list[dict[str, str]]]:
    events: list[dict[str, str]] = []
    reg = ToolRegistry(audit_sink=events.append)
    reg.register_tool("dummy", {"dummy.use"}, confirm, args_schema=Args)
    return reg, events


def test_global_registry_is_empty_and_unknown_tool_denied() -> None:
    assert REGISTRY.names() == []
    user = ToolUser(uuid4(), frozenset({"dummy.use"}))
    run = RunContext(uuid4(), enabled=frozenset({"dummy"}))
    assert REGISTRY.authorize(user, uuid4(), run, "dummy", {"title": "t"}).reason == "unknown_tool"


def test_registered_tool_needs_run_enable_scope_budget_and_valid_args() -> None:
    reg, events = _setup()
    user, ws = ToolUser(uuid4(), frozenset({"dummy.use"})), uuid4()
    args = {"title": "t"}
    assert (
        reg.authorize(user, ws, RunContext(uuid4()), "dummy", args).reason == "not_enabled_for_run"
    )
    run = RunContext(uuid4(), enabled=frozenset({"dummy"}), max_calls=1)
    noscope = ToolUser(uuid4(), frozenset())
    assert reg.authorize(noscope, ws, run, "dummy", args).reason == "missing_scope"
    assert reg.authorize(user, ws, run, "dummy", {"title": 1}).reason == "invalid_arguments"
    assert reg.authorize(user, ws, run, "dummy", {"title": "t", "x": 1}).outcome == "deny"
    assert reg.authorize(user, ws, run, "dummy", args).outcome == "allow"
    assert reg.authorize(user, ws, run, "dummy", args).reason == "budget_exhausted"
    assert all(
        set(e) == {"event", "user_id", "workspace_id", "run_id", "tool", "outcome", "reason"}
        for e in events
    )
    assert "title" not in json.dumps(events)  # ids only: no arguments in the audit


def test_confirmation_required_tool_returns_needs_confirmation() -> None:
    reg, _ = _setup(confirm=True)
    run = RunContext(uuid4(), enabled=frozenset({"dummy"}))
    d = reg.authorize(
        ToolUser(uuid4(), frozenset({"dummy.use"})), uuid4(), run, "dummy", {"title": "t"}
    )
    assert d.outcome == "needs_confirmation"


def test_model_reply_naming_a_registered_tool_does_not_reach_it() -> None:
    """A reply that asks for the dummy tool yields text only; authorize is never consulted."""
    _reg, events = _setup()
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        evt = {
            "choices": [
                {
                    "delta": {
                        "content": "call dummy",
                        "tool_calls": [{"function": {"name": "dummy"}}],
                    },
                    "finish_reason": "stop",
                }
            ]
        }
        return httpx.Response(200, content=f"data: {json.dumps(evt)}\n\ndata: [DONE]\n\n".encode())

    spec = ModelSpec(provider="local", model_id="m", context_tokens=4096, max_output_tokens=64)
    model = OpenAICompatibleChat(
        spec, base_url="http://127.0.0.1:1/v1", transport=httpx.MockTransport(handler)
    )
    out = "".join(model.stream([{"role": "user", "content": "use tool dummy now"}]))
    assert out == "call dummy"
    assert "tools" not in seen[0]
    assert "tool_choice" not in seen[0]
    assert events == []  # nothing authorized, nothing executed
