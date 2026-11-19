"""Adapter interfaces of the evaluation harness, plus the baseline adapters built on merged code.

The harness only talks to ``Retriever`` and ``Answerer``. The real M04 modules plug in through
factories named on the command line (``--retriever pkg.mod:factory``); a factory receives the built
``EvalEnv`` (in-memory repository, fake embedder, id maps) and returns the adapter. See
``docs/development/evaluation.md`` for the expected shape of a real adapter.
"""

from __future__ import annotations

import importlib
import re
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from corpus_build import EvalEnv

RRF_K = 60
ABSTAIN_TEXT = "I cannot answer this from the documents available to you."


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk_id: str  # str(UUID) of the indexed chunk
    document_id: str  # manifest document id
    workspace_id: str  # manifest workspace id
    version: int
    text: str
    score: float


@dataclass(frozen=True, slots=True)
class Citation:
    marker: str  # "[1]"
    chunk_id: str
    document_id: str
    excerpt: str  # text quoted from the chunk


@dataclass(frozen=True, slots=True)
class Answer:
    text: str
    citations: list[Citation] = field(default_factory=list)
    status: Literal["answered", "partial", "abstained"] = "answered"


class Retriever(Protocol):
    def retrieve(self, query: str, workspace_ids: Sequence[str]) -> list[RetrievedChunk]: ...


class Answerer(Protocol):
    def answer(self, question: str, retrieved: Sequence[RetrievedChunk]) -> Answer: ...


def load_factory(spec: str, env: EvalEnv, default_module: str) -> Any:
    """Resolve ``pkg.mod:factory`` (bare ``factory`` = in ``default_module``) and call it."""
    module_name, _, attr = spec.partition(":")
    if not attr:
        module_name, attr = default_module, module_name
    factory = getattr(importlib.import_module(module_name), attr)
    return factory(env)


# ------------------------------------------------------------------------------ baselines

_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "the",
        "their",
        "there",
        "this",
        "to",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "you",
        "your",
    ]
)
_WORD = re.compile(r"[^\W_]+")


def content_words(text: str) -> set[str]:
    """Lower-cased words without stop words; a trailing plural ``s`` is dropped (light stemming)."""
    words = {w for w in _WORD.findall(text.lower()) if w not in _STOP}
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words}


def keyword_query(question: str) -> str:
    words = [w for w in _WORD.findall(question.lower()) if w not in _STOP]
    return " ".join(words) or question


def rrf(rankings: Sequence[Sequence[uuid.UUID]], k: int = RRF_K) -> dict[uuid.UUID, float]:
    """Reciprocal-rank fusion of ranked id lists: score = sum 1 / (k + rank)."""
    fused: dict[uuid.UUID, float] = defaultdict(float)
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            fused[key] += 1.0 / (k + rank)
    return dict(fused)


class BaselineRetriever:
    """Hybrid retrieval over ``InMemoryKnowledgeRepository`` + the fake embedder.

    Vector leg + keyword (BM25) leg under the caller's workspace filter, fused locally with RRF,
    identical-content duplicates removed and at most ``max_per_document`` chunks per document.
    """

    def __init__(
        self, env: EvalEnv, *, limit: int = 10, candidates: int = 50, max_per_document: int = 3
    ) -> None:
        self._env = env
        self._limit = limit
        self._candidates = candidates
        self._max_per_document = max_per_document

    def retrieve(self, query: str, workspace_ids: Sequence[str]) -> list[RetrievedChunk]:
        from app.modules.knowledge.ports import SearchFilters

        env = self._env
        by_org: dict[str, list[uuid.UUID]] = defaultdict(list)
        for ws in workspace_ids:
            by_org[env.org_of_workspace[ws]].append(env.workspace_uuid[ws])
        vector = env.embedder.embed_query(query)
        scored: list[tuple[float, str, str]] = []  # (score, chunk uuid, org)
        for org_id, ws_uuids in by_org.items():
            scope = env.scopes[org_id]
            filters = SearchFilters(frozenset(ws_uuids))
            vec = env.repo.search_vector(
                scope, env.space_id, vector, filters, limit=self._candidates
            )
            kw = env.repo.search_keyword(
                scope, keyword_query(query), filters, limit=self._candidates
            )
            fused = rrf([[h.chunk_id for h in vec], [h.chunk_id for h in kw]])
            scored.extend((s, str(cid), org_id) for cid, s in fused.items())
        scored.sort(key=lambda t: (-t[0], t[1]))
        out: list[RetrievedChunk] = []
        seen_hashes: set[str] = set()
        per_doc: dict[str, int] = defaultdict(int)
        for score, chunk_id, _org in scored:
            info = env.chunks[chunk_id]
            if info.content_hash in seen_hashes or per_doc[info.document_id] >= (
                self._max_per_document
            ):
                continue
            seen_hashes.add(info.content_hash)
            per_doc[info.document_id] += 1
            out.append(
                RetrievedChunk(
                    chunk_id=chunk_id,
                    document_id=info.document_id,
                    workspace_id=info.workspace_id,
                    version=info.version,
                    text=info.text,
                    score=score,
                )
            )
            if len(out) >= self._limit:
                break
        return out


_INJECTION = re.compile(
    r"ignore (all |any )?(the )?(previous|prior|above) (instructions|rules)|"
    r"system override|maintenance mode|reveal (the )?(full |system )|system prompt|"
    r"you are now|do not mention (this|the) instruction|end your answer with",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


class ExtractiveAnswerer:
    """No model: quotes the best-matching sentences of the retrieved chunks and cites them.

    Sentences that look like instructions to an assistant are never quoted (the baseline's stand-in
    for the prompt-injection defence of the real prompt/verification layers). It abstains when no
    sentence covers at least ``min_overlap`` of the question's content words.
    """

    def __init__(self, *, min_overlap: float = 0.5, max_sentences: int = 2, top_chunks: int = 5):
        self._min_overlap = min_overlap
        self._max_sentences = max_sentences
        self._top_chunks = top_chunks

    def answer(self, question: str, retrieved: Sequence[RetrievedChunk]) -> Answer:
        wanted = content_words(question)
        candidates: list[tuple[float, int, int, str, RetrievedChunk]] = []
        for c_index, chunk in enumerate(retrieved[: self._top_chunks]):
            for s_index, sentence in enumerate(_SENTENCE.split(chunk.text)):
                sentence = sentence.strip()
                if len(sentence) < 12 or _INJECTION.search(sentence):
                    continue
                overlap = len(wanted & content_words(sentence)) / len(wanted) if wanted else 0.0
                candidates.append((overlap, c_index, s_index, sentence, chunk))
        if not candidates:
            return Answer(ABSTAIN_TEXT, [], "abstained")
        candidates.sort(key=lambda t: (-t[0], t[1], t[2]))
        best = candidates[0][0]
        if best < self._min_overlap:
            return Answer(ABSTAIN_TEXT, [], "abstained")
        picked = [t for t in candidates if t[0] >= max(self._min_overlap, 0.6 * best)]
        picked = picked[: self._max_sentences]
        parts: list[str] = []
        citations: list[Citation] = []
        for number, (_o, _c, _s, sentence, chunk) in enumerate(picked, start=1):
            marker = f"[{number}]"
            parts.append(f"{sentence} {marker}")
            citations.append(Citation(marker, chunk.chunk_id, chunk.document_id, sentence))
        return Answer(" ".join(parts), citations, "answered")


def baseline_retriever(env: EvalEnv) -> Retriever:
    return BaselineRetriever(env)


def baseline_answerer(env: EvalEnv) -> Answerer:
    del env
    return ExtractiveAnswerer()
