"""Evidence-first prompt assembly (M04.02.02, contract section 6). Pure and deterministic.

Layout: system (fixed) -> evidence block (separate user message, fenced) -> fixed assistant ack ->
prior turns -> question. The ack keeps strict user/assistant alternation (Gemma chat templates
reject two user messages in a row). Marker ``n`` refers to ``used_evidence[n-1]``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from app.modules.ai.models.types import ChatMessage, estimate_tokens
from app.modules.ai.prompts import EVIDENCE_ACK, EVIDENCE_HEADER, PROMPT_VERSION, SYSTEM_PROMPT

MAX_QUESTION_CHARS: Final = 4000
MAX_HISTORY_MESSAGE_CHARS: Final = 4000
MAX_TITLE_CHARS: Final = 120
MAX_EVIDENCE: Final = 20
MESSAGE_OVERHEAD_TOKENS: Final = 4


class EvidenceLike(Protocol):
    """Structural view of ``retrieval.Evidence`` (only revalidated evidence may be passed in)."""

    @property
    def evidence_id(self) -> str: ...
    @property
    def chunk_id(self) -> object: ...
    @property
    def document_id(self) -> object: ...
    @property
    def version_id(self) -> object: ...
    @property
    def title(self) -> str: ...
    @property
    def text(self) -> str: ...
    @property
    def score(self) -> float: ...
    @property
    def page_start(self) -> int | None: ...
    @property
    def page_end(self) -> int | None: ...
    @property
    def heading_path(self) -> Sequence[str]: ...


@dataclass(frozen=True)
class HistoryTurn:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class PromptBundle:
    messages: tuple[ChatMessage, ...]
    used_evidence: tuple[EvidenceLike, ...]
    truncated: bool
    history_trimmed: bool
    abstain_reason: Literal["no_evidence", "insufficient_budget"] | None
    prompt_version: str
    estimated_tokens: int

    @property
    def should_call_model(self) -> bool:
        return self.abstain_reason is None

    @property
    def citation_map(self) -> dict[int, str]:
        """marker number -> evidence_id (what ``verify`` accepts as ``citations``)."""
        return {i + 1: e.evidence_id for i, e in enumerate(self.used_evidence)}


# ---------------------------------------------------------------------------- neutralization

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f  ‪-‮⁦-⁩﻿]")  # noqa: RUF001
_ANGLE_RUN = re.compile(r"<{3,}|>{3,}")
_ROLE_TOKEN = re.compile(
    r"<\|[^<>\n]{0,40}\|>|</?(?:s|bos|eos|pad|unk|start_of_turn|end_of_turn|"
    r"start_of_image|end_of_image|system|user|assistant)>|<<\/?SYS>>|\[/?INST\]",
    re.IGNORECASE,
)


def _spread(match: re.Match[str]) -> str:
    return " ".join(match.group())


def _break_role_token(match: re.Match[str]) -> str:
    token = match.group()
    return token[0] + " " + token[1:]


def neutralize(text: str) -> str:
    """Make untrusted text unable to open/close a fence or impersonate a chat role token."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    text = _ROLE_TOKEN.sub(_break_role_token, text)
    text = _ANGLE_RUN.sub(_spread, text)
    return text


def _one_line(text: str, limit: int) -> str:
    text = neutralize(text)
    text = re.sub(r"[\s|]+", " ", text).strip()
    return text[:limit]


def _pages(start: int | None, end: int | None) -> str:
    if start is None:
        return "-"
    if end is None or end == start:
        return f"p.{start}"
    return f"pp.{start}-{end}"


def _strip_markers(text: str) -> str:
    return re.sub(r"\s?\[\d{1,6}(?:\s*,\s*\d{1,6})*\]", "", text)


def _tokens(message: ChatMessage) -> int:
    return estimate_tokens(message["content"]) + MESSAGE_OVERHEAD_TOKENS


def _evidence_block(n: int, e: EvidenceLike) -> str:
    title = _one_line(e.title, MAX_TITLE_CHARS) or "untitled"
    head = f"<<<EVIDENCE {n} | {title} | {_pages(e.page_start, e.page_end)}>>>"
    return f"{head}\n{neutralize(e.text).strip()}\n<<<END {n}>>>"


def _evidence_message(items: Sequence[EvidenceLike]) -> ChatMessage:
    blocks = [_evidence_block(i + 1, e) for i, e in enumerate(items)]
    return {"role": "user", "content": EVIDENCE_HEADER + "\n\n" + "\n\n".join(blocks)}


def _abstain(
    reason: Literal["no_evidence", "insufficient_budget"], truncated: bool
) -> PromptBundle:
    return PromptBundle((), (), truncated, False, reason, PROMPT_VERSION, 0)


def _history_pairs(history: Iterable[HistoryTurn]) -> list[tuple[ChatMessage, ChatMessage]]:
    """Complete (user, assistant) pairs in order; anything unpaired is dropped; last ``turns``."""
    pairs: list[tuple[ChatMessage, ChatMessage]] = []
    pending: ChatMessage | None = None
    for turn in history:
        content = neutralize(_strip_markers(turn.content)).strip()[:MAX_HISTORY_MESSAGE_CHARS]
        if not content:
            pending = None
            continue
        if turn.role == "user":
            pending = {"role": "user", "content": content}
        elif pending is not None:
            pairs.append((pending, {"role": "assistant", "content": content}))
            pending = None
    return pairs


def build_prompt(
    question: str,
    evidence: Sequence[EvidenceLike],
    history: Sequence[HistoryTurn] = (),
    *,
    budget_tokens: int,
    reserve_output_tokens: int = 800,
    history_turns: int = 6,
) -> PromptBundle:
    """Assemble the prompt. Abstains (``abstain_reason`` set, no messages) without evidence."""
    seen: set[str] = set()
    candidates: list[tuple[int, EvidenceLike]] = []
    for index, e in enumerate(evidence):
        if e.evidence_id in seen or not e.text.strip():
            continue
        seen.add(e.evidence_id)
        candidates.append((index, e))
    if not candidates:
        return _abstain("no_evidence", False)
    candidates.sort(key=lambda pair: (-pair[1].score, pair[0]))
    ordered = [e for _, e in candidates]
    truncated = len(ordered) > MAX_EVIDENCE
    ordered = ordered[:MAX_EVIDENCE]

    q = neutralize(question).strip()
    if len(q) > MAX_QUESTION_CHARS:
        q = q[:MAX_QUESTION_CHARS]
        truncated = True
    if not q:
        return _abstain("no_evidence", truncated)

    system: ChatMessage = {"role": "system", "content": SYSTEM_PROMPT}
    ack: ChatMessage = {"role": "assistant", "content": EVIDENCE_ACK}
    question_msg: ChatMessage = {"role": "user", "content": q}
    available = budget_tokens - reserve_output_tokens
    fixed = _tokens(system) + _tokens(ack) + _tokens(question_msg)

    # Drop the lowest-scored evidence until the evidence message fits; chunks are never cut.
    while ordered and fixed + _tokens(_evidence_message(ordered)) > available:
        ordered.pop()
        truncated = True
    if not ordered:
        return _abstain("insufficient_budget", True)

    evidence_msg = _evidence_message(ordered)
    remaining = available - fixed - _tokens(evidence_msg)
    all_pairs = _history_pairs(history)
    total_pairs = len(all_pairs)
    pairs = all_pairs[-history_turns:] if history_turns > 0 else []
    kept: list[tuple[ChatMessage, ChatMessage]] = []
    for pair in reversed(pairs):  # newest first; stop at the first pair that does not fit
        cost = _tokens(pair[0]) + _tokens(pair[1])
        if cost > remaining:
            break
        kept.append(pair)
        remaining -= cost
    kept.reverse()

    messages: list[ChatMessage] = [system, evidence_msg, ack]
    for user_msg, assistant_msg in kept:
        messages.extend((user_msg, assistant_msg))
    messages.append(question_msg)
    return PromptBundle(
        messages=tuple(messages),
        used_evidence=tuple(ordered),
        truncated=truncated,
        history_trimmed=len(kept) < total_pairs,
        abstain_reason=None,
        prompt_version=PROMPT_VERSION,
        estimated_tokens=sum(_tokens(m) for m in messages),
    )
