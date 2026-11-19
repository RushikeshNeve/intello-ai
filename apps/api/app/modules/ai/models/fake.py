"""Scripted, deterministic chat model for tests of every other module (no network)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from app.modules.ai.models.errors import ChatModelError
from app.modules.ai.models.types import ChatMessage, ModelSpec

DEFAULT_FAKE_SPEC = ModelSpec(
    provider="local", model_id="fake-chat", context_tokens=4096, max_output_tokens=800
)


@dataclass(frozen=True)
class FakeReply:
    """``deltas`` are yielded in order; then ``error`` (if any) is raised."""

    deltas: tuple[str, ...] = ()
    error: ChatModelError | None = None

    @classmethod
    def text(cls, text: str, *, chunk: int = 7) -> FakeReply:
        return cls(tuple(text[i : i + chunk] for i in range(0, len(text), chunk)))


@dataclass(frozen=True)
class FakeCall:
    messages: tuple[ChatMessage, ...]
    temperature: float
    max_tokens: int | None
    stop: tuple[str, ...] | None


Reply = FakeReply | str
ReplyFn = Callable[[Sequence[ChatMessage]], Reply]


class FakeChatModel:
    """``replies``: one per call (the last repeats); or ``reply_fn(messages)`` to script a model
    that reacts to the prompt (e.g. one that obeys injected instructions)."""

    def __init__(
        self,
        replies: Sequence[Reply] = ("",),
        *,
        reply_fn: ReplyFn | None = None,
        spec: ModelSpec = DEFAULT_FAKE_SPEC,
    ) -> None:
        self.spec = spec
        self._replies = list(replies)
        self._reply_fn = reply_fn
        self.calls: list[FakeCall] = []

    def _next(self, messages: Sequence[ChatMessage]) -> FakeReply:
        reply: Reply
        if self._reply_fn is not None:
            reply = self._reply_fn(messages)
        else:
            index = min(len(self.calls) - 1, len(self._replies) - 1)
            reply = self._replies[index]
        return FakeReply.text(reply) if isinstance(reply, str) else reply

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: Sequence[str] | None = None,
    ) -> Iterator[str]:
        self.calls.append(
            FakeCall(tuple(messages), temperature, max_tokens, tuple(stop) if stop else None)
        )
        reply = self._next(messages)
        yield from reply.deltas
        if reply.error is not None:
            raise reply.error

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        return "".join(self.stream(messages, temperature=temperature, max_tokens=max_tokens))
