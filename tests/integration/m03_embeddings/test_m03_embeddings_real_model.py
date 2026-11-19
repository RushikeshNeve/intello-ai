"""M03.02.05 real-model check against LM Studio (optional: skipped when it is not reachable).

Embeds a small fixture with the REAL ``google/embedding-gemma-300m`` through the production
``EmbeddingClient`` and asserts the dimension/model-space contract and basic semantic sanity.

LM Studio runs on the Windows host. From the Linux runner container reach it as
``http://host.docker.internal:1234/v1`` (the default here); override with
``UKOS_TEST_AI_BASE_URL``. The model is JIT-loaded by LM Studio on the first call (no settings are
changed). Run (numbers are printed with ``-s``)::

    scripts/dev/linux-runner/run.sh apps/api \\
      "python -m pytest -q -s -p no:cacheprovider ../../tests/integration/m03_embeddings"
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterator

import httpx
import pytest
from app.modules.ai.embeddings import DEFAULT_SPEC, EmbeddingClient

BASE_URL = os.environ.get("UKOS_TEST_AI_BASE_URL", "http://host.docker.internal:1234/v1")


def _skip_reason() -> str | None:
    try:
        response = httpx.get(f"{BASE_URL}/models", timeout=3.0)
        response.raise_for_status()
        ids = {item["id"] for item in response.json()["data"]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return f"LM Studio not reachable at {BASE_URL}/models ({type(exc).__name__})"
    if DEFAULT_SPEC.model_id not in ids:
        return f"{DEFAULT_SPEC.model_id} is not listed by the server"
    return None


_REASON = _skip_reason()
pytestmark = pytest.mark.skipif(_REASON is not None, reason=_REASON or "")


@pytest.fixture(scope="module")
def client() -> Iterator[EmbeddingClient]:
    # The first call may JIT-load the model: allow a long read timeout.
    with EmbeddingClient(DEFAULT_SPEC, base_url=BASE_URL, timeout_seconds=180.0) as c:
        yield c


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True)) / (math.hypot(*a) * math.hypot(*b))


def test_dimension_and_model_space(client: EmbeddingClient) -> None:
    texts = ["Rotate API keys every ninety days.", "Paris is the capital of France."]
    result = client.embed_documents_detailed(texts)
    assert len(result.vectors) == 2
    for vector in result.vectors:
        assert len(vector) == 768 == DEFAULT_SPEC.dimensions
        assert all(math.isfinite(x) for x in vector)
        assert math.isclose(math.hypot(*vector), 1.0, abs_tol=1e-6)
    assert result.truncated == ()
    assert result.usage.estimated_tokens > 0  # the server reports 0; we count client-side
    assert DEFAULT_SPEC.space_id == "eb3ad37f8d916"
    query = client.embed_query("how often should keys be rotated")
    assert len(query) == 768


def test_paraphrase_is_closer_than_unrelated(client: EmbeddingClient) -> None:
    anchor, paraphrase, unrelated = client.embed_documents(
        [
            "How do I reset my account password?",
            "What are the steps to change a forgotten login password?",
            "The mitochondria is the powerhouse of the cell.",
        ]
    )
    close, far = cosine(anchor, paraphrase), cosine(anchor, unrelated)
    print(f"\n[real-model] paraphrase cosine={close:.4f} unrelated cosine={far:.4f}")
    assert close > far


def test_query_prefix_retrieval_ranks_the_relevant_document_first(client: EmbeddingClient) -> None:
    docs = client.embed_documents(
        [
            "Webhook signatures are verified with an HMAC over the raw request body.",
            "The cafeteria menu changes every Tuesday.",
        ]
    )
    query = client.embed_query("how do I verify a webhook signature")
    relevant, irrelevant = cosine(query, docs[0]), cosine(query, docs[1])
    print(f"\n[real-model] query/relevant={relevant:.4f} query/irrelevant={irrelevant:.4f}")
    assert relevant > irrelevant


def test_multilingual_pair_is_closer_than_unrelated(client: EmbeddingClient) -> None:
    en, de, de_unrelated = client.embed_documents(
        [
            "The weather is beautiful today.",
            "Das Wetter ist heute wunderschön.",
            "Die Rechnung wurde gestern von der Buchhaltung bezahlt.",
        ]
    )
    close, far = cosine(en, de), cosine(en, de_unrelated)
    print(f"\n[real-model] en/de translation cosine={close:.4f} en/de-unrelated cosine={far:.4f}")
    assert close > far


def test_batch_order_matches_single_calls(client: EmbeddingClient) -> None:
    texts = ["first sentence about cats", "second sentence about taxes", "third about volcanoes"]
    batch = client.embed_documents(texts)
    for text, vector in zip(texts, batch, strict=True):
        single = client.embed_documents([text])[0]
        assert cosine(vector, single) > 0.999
