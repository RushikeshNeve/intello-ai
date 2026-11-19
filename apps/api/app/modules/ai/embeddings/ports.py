"""The embedding provider port and its result types (shared by the real client and the fake)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.modules.ai.embeddings.registry import EmbeddingModelSpec


@dataclass(frozen=True)
class EmbeddingUsage:
    """Client-side usage summary. Servers (LM Studio) report ``prompt_tokens: 0``, so tokens are
    estimated as ``ceil(chars / 4)`` over the exact text sent (prefix included, after truncation).
    """

    requests: int = 0
    inputs: int = 0
    estimated_tokens: int = 0
    truncated_inputs: int = 0

    def __add__(self, other: EmbeddingUsage) -> EmbeddingUsage:
        return EmbeddingUsage(
            self.requests + other.requests,
            self.inputs + other.inputs,
            self.estimated_tokens + other.estimated_tokens,
            self.truncated_inputs + other.truncated_inputs,
        )


@dataclass(frozen=True)
class EmbeddingBatchResult:
    """``vectors[i]`` belongs to input ``i``; ``truncated`` lists the input indexes that were cut
    to ``max_input_chars`` (ascending)."""

    vectors: list[list[float]]
    truncated: tuple[int, ...]
    usage: EmbeddingUsage


@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def spec(self) -> EmbeddingModelSpec: ...

    @property
    def usage(self) -> EmbeddingUsage: ...

    def embed_documents_detailed(self, texts: list[str]) -> EmbeddingBatchResult: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def close(self) -> None: ...
