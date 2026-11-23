"""Outbound PII guard (M14.04): wraps a HOSTED chat model so no raw PII or secret leaves.

Applied by ``get_chat_model`` only when the org policy ``pii_outbound`` is ``redact`` or ``block``
(default ``off``: nothing is wrapped) and the model is hosted. The placeholder mapping stays in this
process: it is never sent to the provider. Originals are restored in the answer only when the policy
says ``pii_restore``. Errors carry counts by type at most, never matched values.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from app.modules.ai.models.errors import ChatModelError
from app.modules.ai.models.types import ChatMessage, ChatModel, ModelSpec
from app.modules.firewall.pii import Redactor
from app.modules.firewall.pii.detectors import SECRET_TYPES

PII_BLOCKED = "pii_blocked"


class PiiBlocked(ChatModelError):
    """The prompt contains PII/secrets the org policy does not let leave. Safe to show."""

    code = PII_BLOCKED

    def __init__(self, counts_by_type: dict[str, int]) -> None:
        super().__init__(
            "The request contains sensitive data that cannot be sent to this provider."
        )
        self.counts_by_type = dict(counts_by_type)


class PiiGuardedChatModel:
    def __init__(
        self,
        inner: ChatModel,
        *,
        mode: str = "redact",
        restore: bool = False,
        secrets: str = "block",
    ) -> None:
        if mode not in ("redact", "block"):
            raise ValueError("mode must be redact or block")
        self._inner = inner
        self._mode = mode
        self._restore = restore
        self._secrets = secrets
        self.spec: ModelSpec = inner.spec

    def __getattr__(self, name: str) -> object:  # last_finish_reason, close(), ...
        return getattr(self._inner, name)

    def _prepare(self, messages: Sequence[ChatMessage]) -> tuple[list[ChatMessage], Redactor]:
        redactor = Redactor("placeholder")
        out: list[ChatMessage] = []
        totals: dict[str, int] = {}
        for m in messages:
            res = redactor.redact(m["content"])
            for k, v in res.counts_by_type.items():
                totals[k] = totals.get(k, 0) + v
            out.append({"role": m["role"], "content": res.text})
        if totals:
            has_secret = any(t in SECRET_TYPES for t in totals)
            if self._mode == "block" or (has_secret and self._secrets == "block"):
                raise PiiBlocked(totals)
        return out, redactor

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: Sequence[str] | None = None,
    ) -> Iterator[str]:
        safe, redactor = self._prepare(messages)
        inner = self._inner.stream(safe, temperature=temperature, max_tokens=max_tokens, stop=stop)
        if not (self._restore and redactor.mapping):
            yield from inner
            return
        pending = ""
        for delta in inner:
            pending += delta
            cut = pending.rfind("[")
            if cut != -1 and "]" not in pending[cut:] and len(pending) - cut <= 40:
                emit, pending = pending[:cut], pending[cut:]  # maybe half a placeholder
            else:
                emit, pending = pending, ""
            if emit:
                yield redactor.restore(emit)
        if pending:
            yield redactor.restore(pending)

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        return "".join(self.stream(messages, temperature=temperature, max_tokens=max_tokens))
