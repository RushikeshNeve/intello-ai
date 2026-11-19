"""Optional OpenAI provider (DC-020): same wire protocol, key from ``OPENAI_API_KEY``.

Constructed only by ``get_chat_model`` after the environment switch, the org policy and the key
check all passed. The key lives only in the Authorization header; it is never logged or in errors.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import httpx

from app.modules.ai.models.errors import ChatConfigurationError
from app.modules.ai.models.settings import ChatSettings
from app.modules.ai.models.types import ModelSpec
from app.modules.ai.models.wire import OpenAICompatibleChat


class OpenAIChatModel(OpenAICompatibleChat):
    provider = "openai"
    max_tokens_field = "max_completion_tokens"

    @classmethod
    def from_settings(
        cls,
        settings: ChatSettings,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        transport: httpx.BaseTransport | None = None,
    ) -> OpenAIChatModel:
        key = settings.openai_key()
        if not key:
            raise ChatConfigurationError("OPENAI_API_KEY is not set")
        spec = ModelSpec(
            provider="openai",
            model_id=settings.OPENAI_CHAT_MODEL,
            context_tokens=settings.OPENAI_CONTEXT_TOKENS,
            max_output_tokens=settings.CHAT_MAX_OUTPUT_TOKENS,
        )
        return cls(
            spec,
            base_url=settings.OPENAI_BASE_URL,
            api_key=key,
            timeout_seconds=settings.CHAT_TIMEOUT_SECONDS,
            connect_timeout_seconds=settings.CHAT_CONNECT_TIMEOUT_SECONDS,
            generation_timeout_seconds=settings.CHAT_GENERATION_TIMEOUT_SECONDS,
            max_attempts=settings.CHAT_MAX_ATTEMPTS,
            allow_external=True,
            sleeper=sleeper,
            jitter=jitter,
            transport=transport,
        )
