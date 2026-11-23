"""M14.01 input guard (S1): layered screening of user messages and retrieved evidence.

Layer 1: deterministic rules on a normalized copy of the text (NFKC, invisible characters removed,
case-folded, whitespace collapsed) plus rules on the raw text (invisible/bidi Unicode, encoded
payload markers). Layer 2: a pluggable ``classifier(text) -> score in [0, 1]`` (no-op default, see
:func:`set_classifier`). The score of a text is the strongest rule weight (+0.05 per further rule)
or the classifier score, whichever is higher; it is a *hit* when ``score >= threshold``.

A decision holds counts and rule ids only; the text itself is never stored or logged.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.modules.firewall.policy import BLOCK, OFF

Classifier = Callable[[str], float]


def _noop_classifier(_text: str) -> float:
    return 0.0


_classifier: Classifier = _noop_classifier


def set_classifier(fn: Classifier | None) -> None:
    """Install a model-based classifier (``None`` restores the no-op default)."""
    global _classifier
    _classifier = fn or _noop_classifier


_INVISIBLE = re.compile("[­؜᠎​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")
_WS = re.compile(r"\s+")


def normalize(value: str) -> str:
    return _WS.sub(" ", _INVISIBLE.sub("", unicodedata.normalize("NFKC", value))).casefold()


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    weight: float
    pattern: re.Pattern[str]
    raw: bool = False  # matched on the raw text instead of the normalized copy


def _r(rule_id: str, weight: float, pattern: str, *, raw: bool = False) -> Rule:
    return Rule(rule_id, weight, re.compile(pattern, re.IGNORECASE if raw else 0), raw)


RULES: Final[tuple[Rule, ...]] = (
    _r(
        "ignore_previous",
        0.9,
        r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b(previous|prior|above|"
        r"earlier|preceding|all|any|your)\b[^.\n]{0,30}\b(instructions?|prompts?|rules?|"
        r"directions?|guidelines?|context)\b",
    ),
    _r("new_instructions", 0.6, r"\b(new|updated|real) (system )?instructions?\s*:"),
    _r(
        "system_prompt_exfil",
        0.9,
        r"\b(reveal|print|show|repeat|output|display|leak|dump|recite|disclose|tell me)\b"
        r"[^.\n]{0,50}\b(system|initial|hidden|original|developer)\s+(prompt|instructions?|"
        r"message|rules)\b",
    ),
    _r(
        "system_prompt_exfil_alt",
        0.8,
        r"\b(what (is|are|were)|repeat|reveal)\b[^.\n]{0,30}\byour (system |initial |hidden )?"
        r"(prompt|instructions|rules)\b",
    ),
    _r(
        "role_play_jailbreak",
        0.8,
        r"\b(you are now|from now on you are|pretend (to be|you are)|act as)\b[^.\n]{0,60}"
        r"\b(dan|unrestricted|unfiltered|jailbroken|without (any )?(restrictions|filters|rules|"
        r"limits))\b|\bdo anything now\b|\bdeveloper mode\b|\bjailbreak(ed)?\b",
    ),
    _r(
        "role_tokens",
        0.7,
        r"<\|(im_start|im_end|system|user|assistant|endoftext)\|>|\[/?inst\]|<<sys>>|"
        r"<start_of_turn>|</s>",
    ),
    _r("fence_forgery", 0.8, r"<<<\s*(evidence|end)\b"),
    _r("tool_invocation", 0.5, r"\b(call|invoke|run|execute) the (tool|function|command)\b"),
    _r("exfil_markdown", 0.5, r"!\[[^\]]*\]\(https?://[^)]*\?[^)]*=", raw=True),
    _r("bidi_unicode", 0.6, "[‪-‮⁦-⁩]", raw=True),
    _r("tag_unicode", 0.9, "[\U000e0000-\U000e007f]", raw=True),
    _r("invisible_unicode", 0.6, "[​-‏⁠﻿]{3,}", raw=True),
    _r("encoded_payload_marker", 0.5, r"\b(base64|rot13)\s*[:=]|\bdecode (the|this) following\b"),
    _r("long_base64_run", 0.4, r"[A-Za-z0-9+/]{120,}={0,2}", raw=True),
    _r("hex_escape_run", 0.4, r"(\\x[0-9a-f]{2}){12,}", raw=True),
)


@dataclass(frozen=True, slots=True)
class Decision:
    score: float
    rule_ids: tuple[str, ...]
    hit: bool


CLEAN: Final = Decision(0.0, (), False)


def scan(value: str, threshold: float, *, classifier: Classifier | None = None) -> Decision:
    """Score one text. May raise if a custom classifier raises (the caller decides, fail-safe)."""
    norm = normalize(value)
    found = [r for r in RULES if r.pattern.search(value if r.raw else norm)]
    ids = {r.id for r in found}
    score = 0.0
    if found:
        score = min(1.0, max(r.weight for r in found) + 0.05 * (len(found) - 1))
    score = max(score, float((classifier or _classifier)(value)))
    hit = score >= threshold
    if hit and not ids:
        ids.add("classifier")
    return Decision(round(score, 3), tuple(sorted(ids)), hit)


@dataclass(frozen=True, slots=True)
class EvidenceScreen:
    kept: list[Any]
    dropped: int
    flagged: int
    rule_ids: tuple[str, ...]
    failed: bool = False


def screen_evidence(evidence: Sequence[Any], mode: str, threshold: float) -> EvidenceScreen:
    """Screen each chunk (``.text``). ``block`` drops hostile chunks, ``flag`` only counts them.

    Fail-safe: when scanning a chunk raises, that chunk is dropped in ``block`` mode and kept in
    ``flag`` mode (``failed`` is set)."""
    if mode == OFF:
        return EvidenceScreen(list(evidence), 0, 0, ())
    kept: list[Any] = []
    dropped = flagged = 0
    rules: set[str] = set()
    failed = False
    for ev in evidence:
        try:
            decision = scan(str(ev.text), threshold)
        except Exception:  # noqa: BLE001 - fail safe per mode
            failed = True
            if mode == BLOCK:
                dropped += 1
            else:
                kept.append(ev)
            continue
        if not decision.hit:
            kept.append(ev)
            continue
        rules.update(decision.rule_ids)
        if mode == BLOCK:
            dropped += 1
        else:
            flagged += 1
            kept.append(ev)
    return EvidenceScreen(kept, dropped, flagged, tuple(sorted(rules)), failed)


def screen_user(value: str, mode: str, threshold: float) -> Decision:
    """Decision for a user message; ``off`` never scans. Scan errors: ``block`` -> a hit with rule
    id ``guard_error`` (fail safe), ``flag`` -> clean."""
    if mode == OFF:
        return CLEAN
    try:
        return scan(value, threshold)
    except Exception:  # noqa: BLE001 - fail safe per mode
        return Decision(1.0, ("guard_error",), True) if mode == BLOCK else CLEAN
