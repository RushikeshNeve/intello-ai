"""The usage Meter records the adapter's prompt-cache hit (M11.01 + M11.02 follow-up)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from typing import Any

from app.modules.ai.models.types import ChatMessage, ModelSpec
from app.modules.usage.meter import MeteredChatModel
from app.modules.usage.recorder import UsageContext

SPEC = ModelSpec(provider="openai", model_id="m", context_tokens=1000, max_output_tokens=100)
MESSAGES: list[ChatMessage] = [{"role": "user", "content": "hello there"}]


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(self, ctx: UsageContext, **kw: Any) -> None:
        self.calls.append(kw)


class _Model:
    spec = SPEC

    def __init__(self, cached: int | None) -> None:
        self.last_cached_tokens = cached

    def stream(self, messages: Sequence[ChatMessage], **_: Any) -> Iterator[str]:
        yield "ok"

    def complete(self, messages: Sequence[ChatMessage], **_: Any) -> str:
        return "ok"


def _meter(cached: int | None) -> tuple[MeteredChatModel, _Recorder]:
    rec = _Recorder()
    ctx = UsageContext(org_id=uuid.uuid4(), kind="chat")
    return MeteredChatModel(_Model(cached), rec, ctx), rec  # type: ignore[arg-type]


def test_cached_tokens_reported_by_adapter_are_recorded() -> None:
    model, rec = _meter(42)
    list(model.stream(MESSAGES))
    model.complete(MESSAGES)
    assert [c["tokens_cached"] for c in rec.calls] == [42, 42]


def test_missing_cached_tokens_record_zero() -> None:
    model, rec = _meter(None)
    model.complete(MESSAGES)
    assert rec.calls[0]["tokens_cached"] == 0
