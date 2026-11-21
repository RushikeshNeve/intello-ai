"""Structured model output with one bounded repair (M11.06, feature G6).

``generate_structured(gateway, messages, schema)`` asks the model for JSON, strips code fences,
validates against a pydantic ``schema`` and, on failure, makes at most ``max_repairs`` follow-up
calls that carry only the validation error (the original prompt is not echoed again). The result
is a typed ``StructuredResult`` or a ``StructuredError`` with a stable code and no model text.

``gateway`` is anything with ``complete(messages, temperature=, max_tokens=)`` (a ``ChatModel``).
If it exposes ``supports_json_mode = True`` the call also passes ``response_format``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from app.modules.ai.models.types import ChatMessage

_log = logging.getLogger("ukos.ai.structured")
_FENCE: Final = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")

ERR_NOT_JSON: Final = "not_json"
ERR_SCHEMA: Final = "schema_mismatch"
ERR_MODEL: Final = "model_unavailable"


@dataclass(frozen=True, slots=True)
class StructuredError:
    code: str
    detail: str = ""  # short validator summary, never model text


@dataclass(frozen=True, slots=True)
class StructuredResult[T: BaseModel]:
    value: T | None
    error: StructuredError | None = None
    repaired: bool = False

    @property
    def ok(self) -> bool:
        return self.value is not None


def parse_structured[T: BaseModel](raw: str, schema: type[T]) -> T | StructuredError:
    """Strip fences, load the outermost JSON object, validate. Never raises."""
    text = _FENCE.sub("", (raw or "").strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return StructuredError(ERR_NOT_JSON, "no JSON object found")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return StructuredError(ERR_NOT_JSON, "invalid JSON")
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        parts = [
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()
        ]
        return StructuredError(ERR_SCHEMA, "; ".join(parts[:5])[:300])


def _count(name: str) -> None:
    try:
        from prometheus_client import Counter

        from app.core.tracing import metrics

        m = metrics()
        if name not in m:
            m[name] = Counter(f"ukos_structured_{name}_total", f"Structured output {name}")
        m[name].inc()
    except Exception:  # noqa: BLE001 - metrics must never break generation
        _log.debug("structured metric unavailable", exc_info=True)


def generate_structured[T: BaseModel](
    gateway: Any,
    messages: Sequence[ChatMessage],
    schema: type[T],
    *,
    max_repairs: int = 1,
    temperature: float = 0.0,
    max_tokens: int | None = None,
) -> StructuredResult[T]:
    extra: dict[str, Any] = {}
    if getattr(gateway, "supports_json_mode", False):
        extra["response_format"] = {"type": "json_object"}
    convo: list[ChatMessage] = list(messages)
    repaired = False
    for attempt in range(max_repairs + 1):
        try:
            raw = gateway.complete(convo, temperature=temperature, max_tokens=max_tokens, **extra)
        except Exception:
            if attempt == 0:
                raise
            _count("failed")
            return StructuredResult(None, StructuredError(ERR_MODEL), repaired)
        parsed = parse_structured(raw, schema)
        if not isinstance(parsed, StructuredError):
            if repaired:
                _count("repaired")
            return StructuredResult(parsed, None, repaired)
        _count("parse_failures")
        if attempt >= max_repairs:
            _count("failed")
            return StructuredResult(None, parsed, repaired)
        repaired = True
        convo = [
            *messages,
            {"role": "assistant", "content": (raw or "")[:2000]},
            {
                "role": "user",
                "content": f"Your reply was invalid ({parsed.code}: {parsed.detail}). "
                "Reply again with ONLY the corrected JSON object.",
            },
        ]
    return StructuredResult(None, StructuredError(ERR_SCHEMA), repaired)  # pragma: no cover
