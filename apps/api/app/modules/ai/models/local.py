"""Local provider: any OpenAI-compatible server (LM Studio by default, DC-011).

A non-local base URL is refused unless ``allow_external`` (the gateway passes it only when the
environment switch AND the org policy both allow it).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import httpx

from app.modules.ai.models.settings import ChatSettings
from app.modules.ai.models.types import ModelSpec
from app.modules.ai.models.wire import OpenAICompatibleChat


class LocalChatModel(OpenAICompatibleChat):
    provider = "local"

    @classmethod
    def from_settings(
        cls,
        settings: ChatSettings,
        *,
        allow_external: bool = False,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        transport: httpx.BaseTransport | None = None,
    ) -> LocalChatModel:
        spec = ModelSpec(
            provider="local",
            model_id=settings.CHAT_MODEL,
            context_tokens=settings.CHAT_CONTEXT_TOKENS,
            max_output_tokens=settings.CHAT_MAX_OUTPUT_TOKENS,
        )
        return cls(
            spec,
            base_url=settings.AI_BASE_URL,
            api_key=settings.AI_API_KEY.get_secret_value() if settings.AI_API_KEY else None,
            timeout_seconds=settings.CHAT_TIMEOUT_SECONDS,
            connect_timeout_seconds=settings.CHAT_CONNECT_TIMEOUT_SECONDS,
            generation_timeout_seconds=settings.CHAT_GENERATION_TIMEOUT_SECONDS,
            max_attempts=settings.CHAT_MAX_ATTEMPTS,
            allow_external=allow_external,
            sleeper=sleeper,
            jitter=jitter,
            transport=transport,
        )
