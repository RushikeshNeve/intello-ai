"""M11.02 prompt-cache prefix + cached-token capture; M11.05 warm-up (never raises)."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from app.modules.ai.context import build_prompt
from app.modules.ai.models.wire import OpenAICompatibleChat
from app.modules.ai.warmup import warm_models, warmup_enabled


@dataclass(frozen=True)
class Ev:
    evidence_id: str
    text: str
    title: str = "Doc"
    chunk_id: object = "c"
    document_id: object = "d"
    version_id: object = "v"
    score: float = 1.0
    page_start: int | None = 1
    page_end: int | None = 1
    heading_path: tuple[str, ...] = ()


def test_stable_prefix_is_identical_across_questions() -> None:
    a = build_prompt("What is X?", [Ev("e1", "X is 1.")], budget_tokens=4096)
    b = build_prompt("Who owns Y?", [Ev("e2", "Y is owned by Z.")], budget_tokens=4096)
    assert a.messages[0] == b.messages[0]
    assert a.messages[0]["role"] == "system"
    assert a.messages[-1]["content"] == "What is X?"  # question last


def test_cached_tokens_captured_from_usage_chunk() -> None:
    events = [
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens_details": {"cached_tokens": 42}}},
    ]
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )
    )
    from app.modules.ai.models.types import ModelSpec

    chat = OpenAICompatibleChat(
        ModelSpec("local", "m", 4096, 100),
        base_url="http://localhost:1234/v1",
        api_key=None,
        transport=transport,
    )
    assert chat.complete([{"role": "user", "content": "q"}]) == "hi"
    assert chat.last_cached_tokens == 42


def test_warmup_sends_both_requests_and_never_raises() -> None:
    seen: list[str] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={})

    assert warm_models(transport=httpx.MockTransport(ok)) == {"chat": True, "embedding": True}
    assert seen == ["/v1/chat/completions", "/v1/embeddings"]

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert warm_models(transport=httpx.MockTransport(boom)) == {"chat": False, "embedding": False}


def test_warmup_enabled_defaults() -> None:
    assert warmup_enabled({"AI_BASE_URL": "http://localhost:1234/v1"})
    assert not warmup_enabled({"AI_BASE_URL": "https://api.example.com/v1"})
    assert not warmup_enabled({"WARMUP_ENABLED": "false"})
