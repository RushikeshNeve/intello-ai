"""Deterministic stub providers with controllable quality (tests and dry runs; no network).

``quality`` is in [0, 1]; the same inputs always give the same outputs. ``simulated_ms`` is added to
the measured latency by the harness so latency columns can be exercised without sleeping.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

_EVIDENCE = re.compile(r"<<<EVIDENCE (\d+) \|[^>]*>>>\n(.*?)\n<<<END \d+>>>", re.S)
_WORD = re.compile(r"[^\W_]+")


def _h(text: str, salt: str = "") -> int:
    return int.from_bytes(hashlib.sha256((salt + text).encode()).digest()[:4], "big")


def _unit(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


class StubChat:
    """Answers from evidence block 1 when the question is 'good', else with unsupported filler."""

    def __init__(self, quality: float = 1.0, *, simulated_ms: float = 0.0, fail_every: int = 0):
        self.quality, self.simulated_ms, self.fail_every = quality, simulated_ms, fail_every
        self.calls = 0
        self.last_usage: dict[str, int] | None = None

    def complete(
        self, messages: Sequence[dict], *, temperature: float = 0.0, max_tokens=None
    ) -> str:
        self.calls += 1
        if self.fail_every and self.calls % self.fail_every == 0:
            raise RuntimeError("stub chat failure")
        question = messages[-1]["content"]
        blocks = _EVIDENCE.findall("\n".join(m["content"] for m in messages))
        good = (_h(question, "chat") % 1000) / 1000 < self.quality
        if good and blocks:
            first = re.split(r"(?<=[.!?])\s+", blocks[0][1].strip())[0]
            return f"{first} [{blocks[0][0]}]"
        return "The quarterly weather report mentions sunny skies over the harbor. [1]"


class StubEmbedder:
    """Feature-hashing embedder; ``noise`` in [0, 1] blends in text-keyed pseudo-random vectors."""

    def __init__(self, dimensions: int = 64, noise: float = 0.0, *, simulated_ms: float = 0.0):
        self.dimensions, self.noise, self.simulated_ms = dimensions, noise, simulated_ms
        self.last_tokens = 0

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dimensions
        for w in _WORD.findall(text.lower()):
            d = hashlib.sha256(w.encode()).digest()
            v[int.from_bytes(d[:4], "big") % self.dimensions] += 1.0 if d[4] % 2 == 0 else -1.0
        v = _unit(v)
        if self.noise <= 0:
            return v
        rnd = _unit([((_h(text, f"n{i}") % 2001) - 1000) / 1000 for i in range(self.dimensions)])
        return _unit([(1 - self.noise) * a + self.noise * b for a, b in zip(v, rnd, strict=True)])

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.last_tokens = sum(-(-len(t) // 4) for t in texts)
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        self.last_tokens = -(-len(text) // 4)
        return self._vec(text)


class StubReranker:
    """``lexical`` ranks by query-word overlap; ``reverse`` deliberately inverts the order."""

    def __init__(self, mode: str = "lexical", *, simulated_ms: float = 0.0):
        self.mode, self.simulated_ms = mode, simulated_ms

    def rerank(self, query: str, candidates: list[tuple[str, str]]) -> list[str]:
        q = set(_WORD.findall(query.lower()))
        if self.mode == "reverse":
            return [cid for cid, _ in reversed(candidates)]
        scored = [
            (-len(q & set(_WORD.findall(t.lower()))), i, cid)
            for i, (cid, t) in enumerate(candidates)
        ]
        return [cid for _, _, cid in sorted(scored)]
