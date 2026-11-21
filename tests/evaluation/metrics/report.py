"""Run the eval set once through recording proxies and aggregate the M12.02 RAG metrics.

Used by ``run_eval.py --metrics`` and by ``budgets.py``. No effect on the gate.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rag_metrics as rm
from adapters import Answer, Answerer, RetrievedChunk, Retriever
from corpus_build import EvalEnv
from loader import Item


@dataclass
class Call:
    question: str
    retrieved: list[RetrievedChunk]
    answer: Answer
    retrieval_ms: float
    answer_ms: float


class Recorder:
    """Wraps a retriever and answerer, recording each (question, retrieved, answer) with timings."""

    def __init__(self, retriever: Retriever, answerer: Answerer) -> None:
        import time

        self._time = time.perf_counter
        self._retriever = retriever
        self._answerer = answerer
        self.calls: list[Call] = []
        self._last: tuple[list[RetrievedChunk], float] = ([], 0.0)

    def retrieve(self, query: str, workspace_ids: Sequence[str]) -> list[RetrievedChunk]:
        t0 = self._time()
        got = self._retriever.retrieve(query, workspace_ids)
        self._last = (got, (self._time() - t0) * 1000)
        return got

    def answer(self, question: str, retrieved: Sequence[RetrievedChunk]) -> Answer:
        t0 = self._time()
        ans = self._answerer.answer(question, retrieved)
        ms = (self._time() - t0) * 1000
        # The harness skips retrieve() for personas without workspaces: then ``_last`` is stale.
        rt = self._last if self._last[0] is retrieved else ([], 0.0)
        self.calls.append(Call(question, list(retrieved), ans, rt[1], ms))
        self._last = ([], 0.0)
        return ans


def per_item(items: Sequence[Item], calls: Sequence[Call], env: EvalEnv) -> list[dict[str, Any]]:
    chunk_text = {cid: info.text for cid, info in env.chunks.items()}
    rows: list[dict[str, Any]] = []
    for item, call in zip(items, calls, strict=True):
        ans = call.answer
        abstained = ans.status == "abstained"
        docs = [c.document_id for c in call.retrieved]
        valid, total = rm.citation_validity(
            ans.text, ans.citations, env.chunks, abstained=abstained
        )
        rows.append(
            {
                "id": item.id,
                "kind": item.kind,
                "context_precision": rm.context_precision(docs, item.expected_docs)
                if item.expected_docs
                else None,
                "context_recall": rm.context_recall(docs, item.expected_docs)
                if item.expected_docs
                else None,
                "faithfulness": rm.faithfulness(
                    ans.text, ans.citations, chunk_text, abstained=abstained
                ),
                "answer_relevance": rm.answer_relevance(
                    item.question, ans.text, abstained=abstained
                ),
                "citations_valid": valid,
                "citations_total": total,
                "abstention_correct": rm.abstention_correct(item.expect_abstain, abstained),
            }
        )
    return rows


def aggregate_rag(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    answerable = [r for r in rows if r["kind"] != "unanswerable"]
    cit_total = sum(r["citations_total"] for r in rows)
    return {
        "items": len(rows),
        "context_precision": rm.mean_defined(r["context_precision"] for r in answerable),
        "context_recall": rm.mean_defined(r["context_recall"] for r in answerable),
        "faithfulness": rm.mean_defined(r["faithfulness"] for r in rows),
        "answer_relevance": rm.mean_defined(r["answer_relevance"] for r in answerable),
        "citation_validity": round(sum(r["citations_valid"] for r in rows) / cit_total, 6)
        if cit_total
        else None,
        "abstention_correctness": round(
            sum(1 for r in rows if r["abstention_correct"]) / len(rows), 6
        )
        if rows
        else None,
    }


def render_markdown(agg: dict[str, Any]) -> str:
    lines = ["# RAG metrics (deterministic lexical proxies)", "", "| metric | value |", "|---|---|"]
    for k, v in agg.items():
        lines.append(f"| {k} | {v if v is None or isinstance(v, int) else f'{v:.4f}'} |")
    return "\n".join(lines) + "\n"
