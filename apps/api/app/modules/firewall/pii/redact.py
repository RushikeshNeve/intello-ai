"""Redaction (M14.04): mask / stable placeholder / salted hash. Never logs matched values."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Final, Literal

from app.modules.firewall.pii.detectors import SECRET_TYPES, Finding, detect

Mode = Literal["mask", "placeholder", "hash"]
MODES: Final = ("mask", "placeholder", "hash")


@dataclass(frozen=True, slots=True)
class PiiPolicy:
    """Which types to act on and the minimum confidence."""

    types: frozenset[str] | None = None  # None = every detector
    min_confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    counts_by_type: dict[str, int]
    # token -> original, kept in memory for an optional restore; never in repr, never sent out.
    _mapping: dict[str, str] = field(default_factory=dict, repr=False, compare=False)

    @property
    def has_secrets(self) -> bool:
        return any(t in SECRET_TYPES for t in self.counts_by_type)


class Redactor:
    """One per request: the same value always gets the same placeholder (``[EMAIL_1]``)."""

    def __init__(self, mode: Mode = "placeholder", policy: PiiPolicy | None = None) -> None:
        if mode not in MODES:
            raise ValueError("unknown redaction mode")
        self.mode = mode
        self.policy = policy or PiiPolicy()
        self._ids: dict[tuple[str, str], str] = {}
        self._per_type: dict[str, int] = {}
        self._salt = os.urandom(8)
        self.mapping: dict[str, str] = {}

    def _token(self, type_: str, value: str) -> str:
        if self.mode == "mask":
            return "[REDACTED]"
        key = (type_, value)
        token = self._ids.get(key)
        if token is None:
            if self.mode == "hash":
                digest = hashlib.sha256(self._salt + value.encode()).hexdigest()[:8]
                token = f"[{type_}:{digest}]"
            else:
                n = self._per_type.get(type_, 0) + 1
                self._per_type[type_] = n
                token = f"[{type_}_{n}]"
            self._ids[key] = token
            self.mapping[token] = value
        return token

    def findings(self, text: str) -> list[Finding]:
        p = self.policy
        return [f for f in detect(text, types=p.types) if f.confidence >= p.min_confidence]

    def redact(self, text: str) -> RedactionResult:
        counts: dict[str, int] = {}
        out: list[str] = []
        pos = 0
        used: dict[str, str] = {}
        for f in self.findings(text):
            token = self._token(f.type, text[f.start : f.end])
            out.append(text[pos : f.start])
            out.append(token)
            pos = f.end
            counts[f.type] = counts.get(f.type, 0) + 1
            if token in self.mapping:
                used[token] = self.mapping[token]
        out.append(text[pos:])
        return RedactionResult("".join(out), counts, used)

    def restore(self, text: str) -> str:
        """Put originals back for the placeholders this redactor issued (placeholder/hash only)."""
        if not self.mapping:
            return text
        rx = re.compile("|".join(re.escape(t) for t in sorted(self.mapping, key=len, reverse=True)))
        return rx.sub(lambda m: self.mapping[m.group()], text)


def redact(
    text: str, mode: Mode = "placeholder", policy: PiiPolicy | None = None
) -> RedactionResult:
    return Redactor(mode, policy).redact(text)


def count_by_type(text: str, policy: PiiPolicy | None = None) -> dict[str, int]:
    """Counts per type only (ingestion metadata): no values, no positions."""
    p = policy or PiiPolicy()
    counts: dict[str, int] = {}
    for f in detect(text, types=p.types):
        if f.confidence >= p.min_confidence:
            counts[f.type] = counts.get(f.type, 0) + 1
    return counts
