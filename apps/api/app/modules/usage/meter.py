"""Transparent wrappers that record exactly one usage event per chat / embedding call."""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from typing import Any

from app.modules.ai.embeddings.ports import EmbeddingBatchResult, EmbeddingProvider
from app.modules.ai.models.types import ChatMessage, ChatModel, ModelSpec, estimate_tokens
from app.modules.usage.recorder import UsageContext, UsageRecorder


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


class MeteredChatModel:
    """Records one event per ``stream`` / ``complete`` call (also when it fails or is cancelled)."""

    def __init__(self, inner: ChatModel, recorder: UsageRecorder, ctx: UsageContext) -> None:
        self._inner = inner
        self._recorder = recorder
        self._ctx = ctx
        self.spec: ModelSpec = inner.spec

    def _emit(self, messages: Sequence[ChatMessage], out_chars: int, t0: float) -> None:
        self._recorder.record(
            self._ctx,
            provider=self._inner.spec.provider,
            model=self._inner.spec.model_id,
            tokens_in=sum(estimate_tokens(m["content"]) for m in messages),
            tokens_out=-(-out_chars // 4),
            latency_ms=_ms(t0),
        )

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: Sequence[str] | None = None,
    ) -> Iterator[str]:
        t0 = time.perf_counter()
        chars = 0
        try:
            for delta in self._inner.stream(
                messages, temperature=temperature, max_tokens=max_tokens, stop=stop
            ):
                chars += len(delta)
                yield delta
        finally:
            self._emit(messages, chars, t0)

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        t0 = time.perf_counter()
        chars = 0
        try:
            text = self._inner.complete(messages, temperature=temperature, max_tokens=max_tokens)
            chars = len(text)
            return text
        finally:
            self._emit(messages, chars, t0)


class MeteredEmbedder:
    """Records one ``embed`` event per embedding call; everything else delegates."""

    def __init__(
        self, inner: EmbeddingProvider, recorder: UsageRecorder, ctx: UsageContext
    ) -> None:
        self._inner = inner
        self._recorder = recorder
        self._ctx = ctx

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _emit(self, tokens: int, t0: float) -> None:
        spec = self._inner.spec
        self._recorder.record(
            self._ctx,
            provider=spec.provider,
            model=spec.model_id,
            tokens_in=tokens,
            tokens_out=0,
            latency_ms=_ms(t0),
        )

    def embed_documents_detailed(self, texts: list[str]) -> EmbeddingBatchResult:
        t0 = time.perf_counter()
        tokens = sum(estimate_tokens(t) for t in texts)
        try:
            result = self._inner.embed_documents_detailed(texts)
            tokens = result.usage.estimated_tokens or tokens
            return result
        finally:
            self._emit(tokens, t0)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        t0 = time.perf_counter()
        try:
            return self._inner.embed_documents(texts)
        finally:
            self._emit(sum(estimate_tokens(t) for t in texts), t0)

    def embed_query(self, text: str) -> list[float]:
        t0 = time.perf_counter()
        try:
            return self._inner.embed_query(text)
        finally:
            self._emit(estimate_tokens(text), t0)


def meter_chat_model(model: ChatModel, recorder: UsageRecorder, ctx: UsageContext) -> ChatModel:
    """Wrap ``model``; any failure to wrap returns the model unmetered (never fails the call)."""
    try:
        return MeteredChatModel(model, recorder, ctx)
    except Exception:  # noqa: BLE001
        return model


def meter_embedder(
    embedder: EmbeddingProvider | None, recorder: UsageRecorder, ctx: UsageContext
) -> EmbeddingProvider | None:
    if embedder is None:
        return None
    try:
        return MeteredEmbedder(embedder, recorder, ctx)
    except Exception:  # noqa: BLE001
        return embedder
