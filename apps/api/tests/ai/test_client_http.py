"""EmbeddingClient against a REAL local HTTP server (stdlib ``http.server`` in a thread)."""

from __future__ import annotations

import math
from collections.abc import Iterator

import httpx
import pytest

from app.modules.ai.embeddings import (
    DEFAULT_SPEC,
    EmbeddingClient,
    EmbeddingDimensionMismatch,
    EmbeddingModelMismatch,
    EmbeddingRequestRejected,
    EmbeddingResponseInvalid,
    EmbeddingUnavailable,
    hash_embed,
)
from tests.ai.support import StubServer, running_stub

DOC = DEFAULT_SPEC.document_prefix


@pytest.fixture
def stub() -> Iterator[StubServer]:
    with running_stub() as server:
        yield server


def client_for(
    stub: StubServer, sleeps: list[float] | None = None, **kwargs: object
) -> EmbeddingClient:
    sink: list[float] = sleeps if sleeps is not None else []
    return EmbeddingClient(
        DEFAULT_SPEC,
        base_url=stub.base_url,
        sleeper=sink.append,
        **kwargs,  # type: ignore[arg-type]
    )


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True)) / (math.hypot(*a) * math.hypot(*b))


def test_happy_path_over_real_http(stub: StubServer) -> None:
    with client_for(stub, api_key="sk-test") as client:
        vectors = client.embed_documents(["rotate the api key", "paris is in france"])
        query = client.embed_query("rotate api keys")
    assert len(vectors) == 2
    assert all(len(v) == 768 and all(math.isfinite(x) for x in v) for v in vectors)
    assert stub.last_authorization == "Bearer sk-test"
    expected = hash_embed(DOC + "rotate the api key", 768)
    assert all(math.isclose(a, b, abs_tol=1e-12) for a, b in zip(vectors[0], expected, strict=True))
    assert cosine(query, vectors[0]) > cosine(query, vectors[1])  # shared words are closer


def test_order_restored_over_real_http(stub: StubServer) -> None:
    stub.reverse_order = True
    texts = [f"distinct{i} words" for i in range(10)]
    with client_for(stub) as client:
        vectors = client.embed_documents(texts)
    for text, vector in zip(texts, vectors, strict=True):
        expected = hash_embed(DOC + text, 768)
        assert math.isclose(
            sum(a * b for a, b in zip(vector, expected, strict=True)), 1.0, abs_tol=1e-9
        )


def test_batches_over_real_http(stub: StubServer) -> None:
    with client_for(stub) as client:
        assert len(client.embed_documents([f"t{i}" for i in range(100)])) == 100
    assert stub.input_batches == [16] * 6 + [4]


def test_transient_failures_then_success(stub: StubServer) -> None:
    stub.fail_next = 2
    sleeps: list[float] = []
    with client_for(stub, sleeps) as client:
        assert len(client.embed_documents(["a"])) == 1
    assert stub.requests == 3
    assert len(sleeps) == 2


def test_retry_budget_exhausted_is_transient(stub: StubServer) -> None:
    stub.fail_next = 99
    with client_for(stub, max_attempts=4) as client, pytest.raises(EmbeddingUnavailable) as info:
        client.embed_documents(["a"])
    assert info.value.transient
    assert stub.requests == 4


@pytest.mark.parametrize("status", [400, 401, 404])
def test_permanent_status_over_real_http(stub: StubServer, status: int) -> None:
    stub.fail_next, stub.fail_status = 1, status
    with client_for(stub) as client, pytest.raises(EmbeddingRequestRejected) as info:
        client.embed_documents(["a"])
    assert not info.value.transient
    assert stub.requests == 1


def test_429_is_retried(stub: StubServer) -> None:
    stub.fail_next, stub.fail_status = 1, 429
    with client_for(stub) as client:
        assert len(client.embed_documents(["a"])) == 1
    assert stub.requests == 2


def test_real_read_timeout_is_transient(stub: StubServer) -> None:
    stub.delay_seconds = 0.6
    with (
        client_for(stub, timeout_seconds=0.1, max_attempts=2) as client,
        pytest.raises(EmbeddingUnavailable) as info,
    ):
        client.embed_documents(["a"])
    assert info.value.reason == "timeout"


def test_connection_refused_is_transient() -> None:
    server = StubServer()
    url = server.base_url  # bound but not serving: close it so the port refuses connections
    server._server.server_close()
    with (
        EmbeddingClient(DEFAULT_SPEC, base_url=url, sleeper=lambda _: None) as client,
        pytest.raises(EmbeddingUnavailable) as info,
    ):
        client.embed_query("a")
    assert info.value.transient


def test_wrong_dimensions_over_real_http(stub: StubServer) -> None:
    stub.wrong_dimensions = True
    with client_for(stub) as client, pytest.raises(EmbeddingDimensionMismatch):
        client.embed_documents(["a"])
    assert stub.requests == 1


def test_wrong_count_over_real_http(stub: StubServer) -> None:
    stub.wrong_count = True
    with client_for(stub) as client, pytest.raises(EmbeddingResponseInvalid):
        client.embed_documents(["a", "b"])


def test_wrong_model_echo_over_real_http(stub: StubServer) -> None:
    stub.echo_model = "someone/else"
    with client_for(stub) as client, pytest.raises(EmbeddingModelMismatch):
        client.embed_query("a")


def test_httpx_is_the_transport_type(stub: StubServer) -> None:
    with client_for(stub) as client:
        assert isinstance(client._http, httpx.Client)
