"""Eval answerer with the M14.01/03 firewall ON in block mode (gate M14: eval gate unchanged with
the firewall off and with it on).

Wraps the baseline extractive answerer with the same guards the chat worker uses: the input guard on
the question (hit = refuse), the evidence guard on retrieved chunks (hostile chunks dropped) and the
streaming output guard on the answer (citation markers limited to the offered evidence; a block
withholds the answer). From ``apps/api``:

    python ../../tests/evaluation/run_eval.py --require-baseline --metrics \
        --answerer firewall_adapter:make_answerer
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from adapters import ABSTAIN_TEXT, Answer, Answerer, ExtractiveAnswerer, RetrievedChunk

from app.modules.firewall import input_guard
from app.modules.firewall.output_guard import Context, OutputBlocked, StreamGuard
from app.modules.firewall.policy import DEFAULT_THRESHOLD

if TYPE_CHECKING:
    from corpus_build import EvalEnv

MODE = "block"


class FirewallAnswerer:
    def __init__(self, inner: Answerer, threshold: float = DEFAULT_THRESHOLD) -> None:
        self._inner = inner
        self._threshold = threshold
        self.stats = {"input_blocked": 0, "evidence_dropped": 0, "output_blocked": 0}

    def answer(self, question: str, retrieved: Sequence[RetrievedChunk]) -> Answer:
        if input_guard.screen_user(question, MODE, self._threshold).hit:
            self.stats["input_blocked"] += 1
            return Answer(ABSTAIN_TEXT, [], "abstained")
        screen = input_guard.screen_evidence(list(retrieved), MODE, self._threshold)
        self.stats["evidence_dropped"] += screen.dropped
        kept: list[RetrievedChunk] = list(screen.kept)
        out = self._inner.answer(question, kept)
        if out.status == "abstained":
            return out
        guard = StreamGuard(
            MODE, Context(allowed_markers=frozenset(range(1, len(kept) + 1)) or frozenset({1}))
        )
        try:
            text = guard.feed(out.text) + guard.finish()
        except OutputBlocked:
            self.stats["output_blocked"] += 1
            return Answer(ABSTAIN_TEXT, [], "abstained")
        return Answer(text, out.citations, out.status)


def make_answerer(env: EvalEnv) -> Answerer:
    del env
    return FirewallAnswerer(ExtractiveAnswerer())
