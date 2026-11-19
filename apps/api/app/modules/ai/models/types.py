"""Shared chat-model types: messages, the model spec, the ``ChatModel`` port, token estimate."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict, runtime_checkable

Provider = Literal["local", "openai"]
Role = Literal["system", "user", "assistant"]


class ChatMessage(TypedDict):
    role: Role
    content: str


@dataclass(frozen=True)
class ModelSpec:
    provider: Provider
    model_id: str
    context_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        if not self.model_id:
            raise ValueError("model_id must not be empty")
        if self.context_tokens < 1 or self.max_output_tokens < 1:
            raise ValueError("token limits must be >= 1")


@runtime_checkable
class ChatModel(Protocol):
    spec: ModelSpec

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: Sequence[str] | None = None,
    ) -> Iterator[str]:
        """Yield text deltas in order. Closing the iterator cancels the request."""
        ...

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str: ...


def estimate_tokens(text: str) -> int:
    """Client-side token estimate: ``ceil(chars / 4)`` (servers report unreliable usage)."""
    return -(-len(text) // 4)
