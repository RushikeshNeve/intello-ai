"""Redaction of audit metadata (contract m01-contracts.md §5).

``redact()`` returns a new, JSON-serializable structure and never mutates its input:

* keys matching ``/(pass|secret|token|key|authorization|cookie)/i`` get the value ``"[REDACTED]"``
  (whatever the value is, including nested dicts/lists);
* strings are truncated to :data:`MAX_STRING_LENGTH` characters; emails are kept as they are;
* string values that look like a bearer credential or a JWT are redacted even under a harmless key;
* non-JSON values are stringified (``UUID``, ``datetime``, enums, ``Decimal``, arbitrary objects);
  ``bytes`` are never stored, only their length; NaN/Infinity floats become strings;
* nesting deeper than :data:`MAX_DEPTH`, more than :data:`MAX_ITEMS` entries per container and
  more than :data:`MAX_NODES` values in total are cut off with explicit markers.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Final

REDACTED: Final = "[REDACTED]"
TRUNCATED: Final = "[TRUNCATED]"
TRUNCATED_KEY: Final = "_truncated"
MAX_STRING_LENGTH: Final = 512
MAX_KEY_LENGTH: Final = 128
MAX_DEPTH: Final = 8
MAX_ITEMS: Final = 100
MAX_NODES: Final = 1000

SENSITIVE_KEY: Final = re.compile(r"pass|secret|token|key|authorization|cookie", re.IGNORECASE)
_CREDENTIAL_VALUE: Final = re.compile(
    r"^\s*bearer\s+[A-Za-z0-9._~+/-]+=*\s*$"  # an Authorization header value
    r"|^\s*basic\s+[A-Za-z0-9+/]{8,}={0,2}\s*$"
    r"|^eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$",  # a compact JWS/JWT
    re.IGNORECASE,
)


def is_sensitive_key(key: object) -> bool:
    return SENSITIVE_KEY.search(str(key)) is not None


class _Budget:
    __slots__ = ("remaining",)

    def __init__(self) -> None:
        self.remaining = MAX_NODES

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _truncate(value: str, limit: int = MAX_STRING_LENGTH) -> str:
    return value if len(value) <= limit else value[:limit]


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return REDACTED if _CREDENTIAL_VALUE.search(value) else _truncate(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return f"[BINARY {len(value)} bytes]"
    if isinstance(value, Enum):
        return _scalar(value.value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - a hostile __str__ must not break audit writes
        text = f"<unprintable {type(value).__name__}>"
    return _scalar(text)


def _walk(value: Any, depth: int, budget: _Budget) -> Any:
    if not budget.take():
        return TRUNCATED
    if isinstance(value, Mapping):
        if depth >= MAX_DEPTH:
            return TRUNCATED
        out: dict[str, Any] = {}
        items = list(value.items())
        kept = 0
        for key, item in items[:MAX_ITEMS]:
            if budget.remaining <= 0:
                break
            name = _truncate(str(key), MAX_KEY_LENGTH)
            out[name] = REDACTED if is_sensitive_key(key) else _walk(item, depth + 1, budget)
            kept += 1
        if len(items) > kept:
            out[TRUNCATED_KEY] = len(items) - kept
        return out
    if isinstance(value, list | tuple | set | frozenset):
        if depth >= MAX_DEPTH:
            return TRUNCATED
        seq = list(value)
        result: list[Any] = []
        for item in seq[:MAX_ITEMS]:
            if budget.remaining <= 0:
                break
            result.append(_walk(item, depth + 1, budget))
        if len(seq) > len(result):
            result.append(f"{TRUNCATED} {len(seq) - len(result)} more")
        return result
    return _scalar(value)


def redact(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a redacted, bounded, JSON-serializable copy of ``metadata`` (``{}`` for None)."""
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise TypeError("audit metadata must be a mapping")
    result = _walk(metadata, 0, _Budget())
    return result if isinstance(result, dict) else {}
