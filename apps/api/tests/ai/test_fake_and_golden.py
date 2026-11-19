"""FakeEmbeddingClient and the cross-check against the Node stub's golden vectors.

Regenerate the golden vectors after a deliberate algorithm change (host, Node 20+; run from the
repository root)::

    node tests/e2e/support/embedding-stub.mjs --vectors apps/api/tests/ai/golden/cases.json \\
        > apps/api/tests/ai/golden/vectors.json

and change ``fake.py`` and ``embedding-stub.mjs`` together. This test fails when they diverge.
"""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path

import pytest

from app.modules.ai.embeddings import (
    DEFAULT_SPEC,
    EmbeddingInputInvalid,
    EmbeddingProvider,
    EmbeddingUnavailable,
    FakeEmbeddingClient,
    hash_embed,
)
from app.modules.ai.embeddings.fake import tokenize

GOLDEN = Path(__file__).parent / "golden"


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_python_fake_and_node_stub_produce_identical_vectors() -> None:
    golden = json.loads((GOLDEN / "vectors.json").read_text(encoding="utf-8"))
    cases = json.loads((GOLDEN / "cases.json").read_text(encoding="utf-8"))["cases"]
    assert golden["algorithm"] == "feature-hashing-v1"
    assert [(c["dimensions"], c["texts"]) for c in golden["cases"]] == [
        (c["dimensions"], c["texts"]) for c in cases
    ], "cases.json changed: regenerate vectors.json (see module docstring)"
    checked = 0
    for case in golden["cases"]:
        for text, expected in zip(case["texts"], case["vectors"], strict=True):
            assert hash_embed(text, case["dimensions"]) == expected, text
            checked += 1
    assert checked >= 12


def test_tokenisation_rules() -> None:
    assert tokenize("Hello, WORLD! snake_case 4711 café") == [
        "hello",
        "world",
        "snake",
        "case",
        "4711",
        "café",
    ]
    assert tokenize("!!! --- ???") == []


def test_hash_embed_is_unit_length_deterministic_and_sized() -> None:
    vector = hash_embed("the quick brown fox", 768)
    assert len(vector) == 768
    assert math.isclose(sum(v * v for v in vector), 1.0, abs_tol=1e-12)
    assert vector == hash_embed("the quick brown fox", 768)
    assert hash_embed("!!!", 8) == [1.0, 0, 0, 0, 0, 0, 0, 0]
    with pytest.raises(ValueError, match="dimensions"):
        hash_embed("x", 0)


def test_shared_words_are_closer_than_unrelated_texts() -> None:
    a = hash_embed("how to rotate an api key safely", 768)
    b = hash_embed("rotate the api key every ninety days", 768)
    c = hash_embed("paris is the capital of france", 768)
    assert cosine(a, b) > cosine(a, c) + 0.2


def test_fake_interface_and_prefix_behaviour() -> None:
    fake = FakeEmbeddingClient()
    assert isinstance(fake, EmbeddingProvider)
    docs = fake.embed_documents(["alpha beta", "gamma"])
    query = fake.embed_query("alpha beta")
    assert len(docs) == 2
    assert all(len(v) == 768 for v in [*docs, query])
    assert docs[0] == hash_embed(DEFAULT_SPEC.document_prefix + "alpha beta", 768)
    assert query == hash_embed(DEFAULT_SPEC.query_prefix + "alpha beta", 768)
    assert fake.embed_documents([]) == []
    assert (fake.document_calls, fake.query_calls) == (2, 1)


def test_fake_query_is_closest_to_the_matching_document() -> None:
    fake = FakeEmbeddingClient()
    docs = fake.embed_documents(
        ["webhook signatures use an hmac", "invoices are paid monthly", "the cat sat on the mat"]
    )
    query = fake.embed_query("how are webhook signatures verified")
    scores = [cosine(query, d) for d in docs]
    assert scores.index(max(scores)) == 0


def test_fake_truncation_usage_and_blank_handling() -> None:
    fake = FakeEmbeddingClient(dataclasses.replace(DEFAULT_SPEC, max_input_chars=5, dimensions=32))
    result = fake.embed_documents_detailed(["abcdefghij", "ok"])
    assert result.truncated == (0,)
    assert result.vectors[0] == hash_embed(DEFAULT_SPEC.document_prefix + "abcde", 32)
    assert result.usage.estimated_tokens == sum(
        -(-n // 4)
        for n in (len(DEFAULT_SPEC.document_prefix) + 5, len(DEFAULT_SPEC.document_prefix) + 2)
    )
    assert fake.usage.inputs == 2
    with pytest.raises(EmbeddingInputInvalid):
        fake.embed_documents([" "])


def test_fake_can_inject_errors_and_close() -> None:
    fake = FakeEmbeddingClient()
    fake.fail_next(EmbeddingUnavailable("boom", attempts=3), times=2)
    with pytest.raises(EmbeddingUnavailable):
        fake.embed_query("a")
    with pytest.raises(EmbeddingUnavailable):
        fake.embed_documents(["a"])
    assert len(fake.embed_query("a")) == 768
    with fake:
        pass
    assert fake.closed
