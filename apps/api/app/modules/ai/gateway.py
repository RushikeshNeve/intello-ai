"""Model gateway (M04.02.01): the single choke point that decides which chat model may be used.

``get_chat_model`` applies, in this order: the environment switch ``AI_EXTERNAL_PROVIDERS_ENABLED``,
the org's ``ModelPolicy``, key presence, and the local-host guard. A denied provider is never
constructed. Every denial is a ``PolicyDenied`` with a stable reason and a user-safe message.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.modules.ai.embeddings.client import is_local_host
from app.modules.ai.models.errors import ChatConfigurationError
from app.modules.ai.models.fake import FakeChatModel, FakeReply
from app.modules.ai.models.local import LocalChatModel
from app.modules.ai.models.openai_adapter import OpenAIChatModel
from app.modules.ai.models.policy import (
    KNOWN_PROVIDERS,
    InMemoryPolicySource,
    ModelPolicy,
    PolicyDenied,
    PolicySource,
)
from app.modules.ai.models.settings import ChatSettings
from app.modules.ai.models.types import (
    ChatMessage,
    ChatModel,
    ModelSpec,
    Provider,
    estimate_tokens,
)

__all__ = [
    "ChatMessage",
    "ChatModel",
    "ChatSettings",
    "FakeChatModel",
    "FakeReply",
    "InMemoryPolicySource",
    "LocalChatModel",
    "ModelPolicy",
    "ModelSpec",
    "OpenAIChatModel",
    "PolicyDenied",
    "PolicySource",
    "estimate_tokens",
    "get_chat_model",
]


def get_chat_model(
    policy_source: PolicySource,
    org_id: UUID,
    settings: ChatSettings | None = None,
    *,
    provider: Provider | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    transport: httpx.BaseTransport | None = None,
) -> ChatModel:
    """Return the chat model this org may use, or raise ``PolicyDenied``.

    ``provider=None`` prefers ``local`` and falls back to ``openai`` only if the org policy allows
    only that. Policy lookup failures fail closed.
    """
    s = settings if settings is not None else ChatSettings()
    try:
        policy = policy_source.get_policy(org_id)
    except Exception:  # noqa: BLE001 - any failure must deny (fail closed)
        raise PolicyDenied("policy_unavailable") from None
    external_possible = s.AI_EXTERNAL_PROVIDERS_ENABLED and policy.allow_external
    allowed = set(policy.allowed_providers) & KNOWN_PROVIDERS

    chosen: str
    if provider is not None:
        if provider not in KNOWN_PROVIDERS:
            raise PolicyDenied("unknown_provider")
        chosen = provider
    elif "local" in allowed:
        chosen = "local"
    elif "openai" in allowed:
        chosen = "openai"
    else:
        raise PolicyDenied("no_allowed_provider")

    if chosen == "openai":
        if not s.AI_EXTERNAL_PROVIDERS_ENABLED:
            raise PolicyDenied("external_disabled")
        if not policy.allow_external:
            raise PolicyDenied("org_policy_denies_external")
        if "openai" not in allowed:
            raise PolicyDenied("provider_not_allowed")
        if not policy.hosted_models_allowed:
            raise PolicyDenied("hosted_not_allowed")
        if not s.openai_key():
            raise PolicyDenied("missing_credentials")
        try:
            hosted: ChatModel = OpenAIChatModel.from_settings(
                s, sleeper=sleeper, jitter=jitter, transport=transport
            )
        except ChatConfigurationError:
            raise PolicyDenied("endpoint_not_local") from None
        if policy.pii_outbound in ("redact", "block"):  # M14.04, default off
            from app.modules.ai.models.pii_guard import PiiGuardedChatModel

            return PiiGuardedChatModel(
                hosted,
                mode=policy.pii_outbound,
                restore=policy.pii_restore,
                secrets=policy.pii_secrets,
            )
        return hosted

    if "local" not in allowed:
        raise PolicyDenied("provider_not_allowed")
    host = urlsplit(s.AI_BASE_URL.strip()).hostname or ""
    if not is_local_host(host) and not external_possible:
        raise PolicyDenied("endpoint_not_local")
    try:
        return LocalChatModel.from_settings(
            s,
            allow_external=external_possible,
            sleeper=sleeper,
            jitter=jitter,
            transport=transport,
        )
    except ChatConfigurationError:
        raise PolicyDenied("endpoint_not_local") from None
