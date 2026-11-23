"""M14.03 output guard (S3): checks a model answer before it leaves the worker.

Findings (rule ids only, never text):

* ``scaffold_canary``: the per-org canary embedded in the fixed scaffold appears in the output;
* ``scaffold_echo``: >= ``ECHO_SHINGLES`` six-word runs of the scaffold appear in the output;
* ``secret_*``: secret-like tokens (cloud/API keys, private keys, JWTs, password assignments);
* ``out_of_scope_citation`` / ``unknown_id``: a citation marker or an id that does not belong to the
  evidence this run was allowed to use (belt and braces on top of citation revalidation).

Modes: ``off`` | ``flag`` (record only) | ``block`` | ``redact`` (secrets, and PII through
``modules.firewall.pii`` when that module exists, are replaced; scaffold/scope findings cannot be
redacted and block). :class:`StreamGuard` runs the same checks incrementally with a hold-back
window, so text that later turns out to be blocked has not been flushed to the client.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, Final

from app.modules.firewall.input_guard import normalize
from app.modules.firewall.policy import BLOCK, FLAG, OFF, REDACT

HOLDBACK: Final = 96  # characters kept back while streaming (covers the longest early-match)
ECHO_SHINGLES: Final = 3
SHINGLE_WORDS: Final = 6
REDACTED: Final = "[redacted]"
_ABSTENTION: Final = "i don't know based on the available documents."


class OutputBlocked(Exception):
    def __init__(self, rule_ids: Collection[str]) -> None:
        super().__init__("output_blocked")
        self.rule_ids = tuple(sorted(rule_ids))


def canary_for(org_id: object) -> str:
    """Deterministic marker embedded in the scaffold; its appearance in an answer is an echo."""
    return "ukcn-" + hashlib.sha256(f"ukos-firewall-canary:{org_id}".encode()).hexdigest()[:20]


def scaffold_line(canary: str) -> str:
    return f"(scaffold marker {canary}: internal, never write it in an answer)"


_SECRETS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("secret_aws_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("secret_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("secret_github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("secret_slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("secret_private_key", re.compile(r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----")),
    ("secret_jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]*)?")),
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*['\"]?[^\s'\"]{12,}"
        ),
    ),
)
_MARKER = re.compile(r"\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


@dataclass(frozen=True, slots=True)
class Context:
    canary: str | None = None
    scaffold: str = ""
    allowed_markers: frozenset[int] | None = None
    allowed_ids: frozenset[str] | None = None
    shingles: frozenset[str] = field(init=False, default=frozenset())

    def __post_init__(self) -> None:
        words = normalize(self.scaffold.lower().replace(_ABSTENTION, " ")).split()
        object.__setattr__(
            self,
            "shingles",
            frozenset(
                " ".join(words[i : i + SHINGLE_WORDS])
                for i in range(max(0, len(words) - SHINGLE_WORDS + 1))
            ),
        )


@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    start: int
    end: int
    redactable: bool


def find(text: str, ctx: Context) -> list[Finding]:
    out: list[Finding] = []
    if ctx.canary and ctx.canary in text:
        out.append(Finding("scaffold_canary", 0, len(text), False))
    if ctx.shingles:
        words = normalize(text).split()
        seen = {
            " ".join(words[i : i + SHINGLE_WORDS])
            for i in range(max(0, len(words) - SHINGLE_WORDS + 1))
        }
        if len(seen & ctx.shingles) >= ECHO_SHINGLES:
            out.append(Finding("scaffold_echo", 0, len(text), False))
    for rule_id, pattern in _SECRETS:
        out.extend(Finding(rule_id, m.start(), m.end(), True) for m in pattern.finditer(text))
    if ctx.allowed_markers is not None:
        for m in _MARKER.finditer(text):
            nums = {int(n) for n in re.findall(r"\d+", m.group(1))}
            if not nums <= ctx.allowed_markers:
                out.append(Finding("out_of_scope_citation", m.start(), m.end(), False))
    if ctx.allowed_ids is not None:
        for m in _UUID.finditer(text):
            if m.group(0).lower() not in ctx.allowed_ids:
                out.append(Finding("unknown_id", m.start(), m.end(), False))
    return out


def try_redact(value: str) -> str:
    """PII redaction through ``modules.firewall.pii`` when it exists; otherwise (or on any
    failure) the text is returned unchanged."""
    try:
        import importlib

        mod: Any = importlib.import_module("app.modules.firewall.pii")
        fn = getattr(mod, "redact", None)
        if fn is None:
            return value
        res = fn(value)
        if isinstance(res, str):
            return res
        inner = getattr(res, "text", None)
        return inner if isinstance(inner, str) else value
    except Exception:  # noqa: BLE001 - pii is optional and must never break the answer
        return value


def _apply(text: str, spans: list[tuple[int, int]]) -> str:
    out: list[str] = []
    pos = 0
    for start, end in sorted(spans):
        if start < pos:
            end = max(end, pos)
            start = pos
            if end <= start:
                continue
        out.append(text[pos:start])
        out.append(REDACTED)
        pos = end
    out.append(text[pos:])
    return "".join(out)


class StreamGuard:
    """Incremental guard. ``feed`` returns the text that is safe to send now; ``finish`` the rest.
    Raises :class:`OutputBlocked` as soon as ``block`` (or an unredactable finding in ``redact``)
    is detected; text still held back is then never emitted."""

    def __init__(self, mode: str, ctx: Context, *, holdback: int = HOLDBACK) -> None:
        self.mode = mode
        self.ctx = ctx
        self.holdback = holdback if mode in (BLOCK, REDACT) else 0
        self.raw = ""
        self._sent = 0
        self.rule_ids: set[str] = set()
        self.findings = 0
        self.redactions = 0

    def summary(self) -> dict[str, Any]:
        return {
            "output": {
                "mode": self.mode,
                "findings": self.findings,
                "redactions": self.redactions,
                "rules": sorted(self.rule_ids),
            }
        }

    def _scan(self) -> list[Finding]:
        try:
            found = find(self.raw, self.ctx)
        except Exception:  # noqa: BLE001 - fail safe per mode
            if self.mode in (BLOCK, REDACT):
                self.rule_ids.add("guard_error")
                raise OutputBlocked(self.rule_ids) from None
            return []
        if found:
            self.rule_ids.update(f.rule_id for f in found)
            self.findings = max(self.findings, len(found))
            if self.mode == BLOCK or (self.mode == REDACT and not all(f.redactable for f in found)):
                raise OutputBlocked(self.rule_ids)
        return found

    def _emit(self, upto: int, found: list[Finding]) -> str:
        spans = [(f.start, f.end) for f in found if f.redactable and f.end <= upto]
        seg_spans = [(s - self._sent, e - self._sent) for s, e in spans if e > self._sent]
        seg = self.raw[self._sent : upto]
        if self.mode == REDACT:
            self.redactions += len(seg_spans)
            seg = try_redact(_apply(seg, [(max(s, 0), e) for s, e in seg_spans]))
        self._sent = upto
        return seg

    def feed(self, piece: str) -> str:
        if self.mode == OFF:
            return piece
        self.raw += piece
        found = self._scan()
        if self.mode == FLAG:
            self._sent = len(self.raw)
            return piece
        upto = max(self._sent, len(self.raw) - self.holdback)
        for f in found:
            if f.redactable and f.start < upto < f.end:
                upto = max(self._sent, f.start)
        return self._emit(upto, found)

    def finish(self) -> str:
        if self.mode in (OFF, FLAG):
            if self.mode == FLAG:
                self._scan()
            return ""
        found = self._scan()
        return self._emit(len(self.raw), found)
