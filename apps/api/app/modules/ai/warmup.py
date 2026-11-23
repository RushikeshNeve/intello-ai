"""Best-effort warm-up of the local model server (M11.05): one tiny chat and one tiny embedding.

Loads the models into LM Studio memory before the first user request. Never raises. Enabled by
``WARMUP_ENABLED`` (default: on when ``AI_BASE_URL`` points at a local host).
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx

from app.modules.ai import egress
from app.modules.ai.embeddings.client import is_local_host
from app.modules.ai.embeddings.settings import EmbeddingSettings
from app.modules.ai.models.settings import ChatSettings

log = logging.getLogger("ukos.ai.warmup")
_TRUE = {"1", "true", "yes", "on"}


def warmup_enabled(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    raw = env.get("WARMUP_ENABLED")
    if raw is not None and raw.strip():
        return raw.strip().lower() in _TRUE
    base = env.get("AI_BASE_URL") or ChatSettings.model_fields["AI_BASE_URL"].default
    return is_local_host(urlsplit(str(base).strip()).hostname or "")


def warm_models(*, transport: httpx.BaseTransport | None = None) -> dict[str, bool]:
    """Send the tiny requests; return ``{"chat": ok, "embedding": ok}``. Never raises."""
    result = {"chat": False, "embedding": False}
    try:
        chat = ChatSettings()
        emb = EmbeddingSettings()
        headers: dict[str, str] = {}
        if chat.AI_API_KEY:
            headers["Authorization"] = f"Bearer {chat.AI_API_KEY.get_secret_value()}"
        base = chat.AI_BASE_URL.rstrip("/")
        calls = (
            (
                "chat",
                "/chat/completions",
                {
                    "model": chat.CHAT_MODEL,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                    "temperature": 0,
                },
            ),
            ("embedding", "/embeddings", {"model": emb.AI_EMBEDDING_MODEL, "input": "hi"}),
        )
        with httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(120.0, connect=5.0),
            transport=transport,
            event_hooks={"request": [egress.request_hook("warmup")]},
        ) as http:
            for key, path, body in calls:
                try:
                    result[key] = http.post(base + path, json=body).status_code == 200
                except Exception as exc:  # noqa: BLE001 - best effort
                    log.info("warm-up %s skipped (%s)", key, type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - never raise
        log.info("warm-up skipped (%s)", type(exc).__name__)
    return result


def start_warmup_background() -> None:
    """Fire-and-forget thread; no-op when disabled."""
    try:
        if warmup_enabled():
            threading.Thread(target=warm_models, name="model-warmup", daemon=True).start()
    except Exception:  # noqa: BLE001, S110
        pass
