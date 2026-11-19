"""EmbeddingClient against ``httpx.MockTransport``: validation, retries, classification, safety."""

from __future__ import annotations

import dataclasses
import json
import logging
import math
from typing import Any

import httpx
import pytest

from app.modules.ai.embeddings import (
    DEFAULT_SPEC,
    EmbeddingClient,
    EmbeddingConfigurationError,
    EmbeddingDimensionMismatch,
    EmbeddingError,
    EmbeddingInputInvalid,
    EmbeddingModelMismatch,
    EmbeddingRequestRejected,
    EmbeddingResponseInvalid,
    EmbeddingSettings,
    EmbeddingUnavailable,
    build_embedding_client,
    hash_embed,
    is_local_host,
)
from tests.ai.support import Recorder, embedding_response, ok

BASE = "http://localhost:1234/v1"
DIM = DEFAULT_SPEC.dimensions
DOC = DEFAULT_SPEC.document_prefix
QUERY = DEFAULT_SPEC.query_prefix


def make(
    recorder: Recorder,
    sleeps: list[float] | None = None,
    spec: Any = DEFAULT_SPEC,
    **kwargs: Any,
) -> EmbeddingClient:
    sink = sleeps if sleeps is not None else []
    kwargs.setdefault("base_url", BASE)
    kwargs.setdefault("jitter", lambda: 1.0)
    return EmbeddingClient(spec, transport=recorder.transport, sleeper=sink.append, **kwargs)


def json_response(body: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)


def status_response(status: int) -> Recorder:
    return Recorder(lambda r, p: httpx.Response(status, text="TOPSECRET body"))


def close(a: list[float], b: list[float]) -> bool:
    return len(a) == len(b) and all(
        math.isclose(x, y, abs_tol=1e-12) for x, y in zip(a, b, strict=True)
    )


# ---------------------------------------------------------------- happy path and request shape


def test_happy_path_request_shape_and_prefix() -> None:
    rec = Recorder(ok)
    vectors = make(rec).embed_documents(["alpha beta", "gamma"])
    assert len(vectors) == 2
    assert all(len(v) == DIM for v in vectors)
    assert str(rec.requests[0].url) == f"{BASE}/embeddings"
    assert rec.requests[0].method == "POST"
    assert rec.payloads == [
        {"model": DEFAULT_SPEC.model_id, "input": [DOC + "alpha beta", DOC + "gamma"]}
    ]
    assert close(vectors[0], hash_embed(DOC + "alpha beta", DIM))


def test_query_uses_the_query_prefix_exactly() -> None:
    rec = Recorder(ok)
    vector = make(rec).embed_query("how to rotate keys?")
    assert rec.sent_inputs == [[QUERY + "how to rotate keys?"]]
    assert len(vector) == DIM


def test_trailing_slash_in_base_url_is_normalised() -> None:
    rec = Recorder(ok)
    make(rec, base_url=BASE + "/").embed_query("x")
    assert str(rec.requests[0].url) == f"{BASE}/embeddings"


def test_order_is_restored_when_the_server_returns_out_of_order() -> None:
    rec = Recorder(
        lambda r, p: json_response(embedding_response(p["input"], reverse=True)),
    )
    texts = [f"word{i} common" for i in range(5)]
    vectors = make(rec).embed_documents(texts)
    for text, vector in zip(texts, vectors, strict=True):
        assert close(vector, hash_embed(DOC + text, DIM))


def test_empty_input_makes_no_call() -> None:
    rec = Recorder(ok)
    client = make(rec)
    assert client.embed_documents([]) == []
    assert rec.requests == []
    assert client.usage.requests == 0


@pytest.mark.parametrize(
    ("count", "requests"), [(0, 0), (1, 1), (15, 1), (16, 1), (17, 2), (32, 2), (33, 3), (100, 7)]
)
def test_batching_boundaries(count: int, requests: int) -> None:
    rec = Recorder(ok)
    texts = [f"text number {i}" for i in range(count)]
    vectors = make(rec).embed_documents(texts)
    assert len(rec.requests) == requests
    assert all(len(batch) <= 16 for batch in rec.sent_inputs)
    assert [t for batch in rec.sent_inputs for t in batch] == [DOC + t for t in texts]
    assert len(vectors) == count
    for i in (0, count - 1):
        if count:
            assert close(vectors[i], hash_embed(DOC + texts[i], DIM))


def test_custom_batch_size() -> None:
    rec = Recorder(ok)
    make(rec, batch_size=3).embed_documents([f"t{i}" for i in range(7)])
    assert [len(b) for b in rec.sent_inputs] == [3, 3, 1]


# ---------------------------------------------------------------- truncation


def test_truncation_is_deterministic_reported_and_applied_before_the_prefix() -> None:
    spec = dataclasses.replace(DEFAULT_SPEC, max_input_chars=10)
    rec = Recorder(ok)
    result = make(rec, spec=spec).embed_documents_detailed(["x" * 25, "y" * 10, "z" * 11, "ok"])
    assert rec.sent_inputs == [[DOC + "x" * 10, DOC + "y" * 10, DOC + "z" * 10, DOC + "ok"]]
    assert result.truncated == (0, 2)
    assert result.usage.truncated_inputs == 2
    assert len(result.vectors) == 4


def test_truncation_counts_characters_not_bytes() -> None:
    spec = dataclasses.replace(DEFAULT_SPEC, max_input_chars=4)
    rec = Recorder(ok)
    make(rec, spec=spec).embed_query("éééééé")
    assert rec.sent_inputs == [[QUERY + "éééé"]]


def test_no_truncation_reported_when_within_limit() -> None:
    result = make(Recorder(ok)).embed_documents_detailed(["short"])
    assert result.truncated == ()


# ---------------------------------------------------------------- response validation


def test_dimension_mismatch_is_an_error_and_returns_nothing() -> None:
    rec = Recorder(lambda r, p: json_response(embedding_response(p["input"], dimensions=DIM - 1)))
    with pytest.raises(EmbeddingDimensionMismatch) as info:
        make(rec).embed_documents(["a"])
    assert (info.value.expected, info.value.actual) == (DIM, DIM - 1)
    assert info.value.transient is False
    assert len(rec.requests) == 1  # permanent: never retried


def test_too_many_dimensions_is_an_error() -> None:
    rec = Recorder(lambda r, p: json_response(embedding_response(p["input"], dimensions=DIM + 1)))
    with pytest.raises(EmbeddingDimensionMismatch):
        make(rec).embed_query("a")


def test_a_bad_second_batch_discards_the_good_first_batch() -> None:
    def respond(request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
        dims = DIM if len(rec.requests) == 1 else DIM - 1
        return json_response(embedding_response(payload["input"], dimensions=dims))

    rec = Recorder(respond)
    client = make(rec)
    with pytest.raises(EmbeddingDimensionMismatch):
        client.embed_documents([f"t{i}" for i in range(20)])
    assert client.usage.inputs == 0  # a failed call counts nothing


def test_spec_dimensions_are_enforced_not_the_default() -> None:
    spec = dataclasses.replace(DEFAULT_SPEC, dimensions=8)
    rec = Recorder(lambda r, p: json_response(embedding_response(p["input"], dimensions=8)))
    assert len(make(rec, spec=spec).embed_query("a")) == 8
    rec2 = Recorder(ok)  # 768 dims from the server, 8 expected
    with pytest.raises(EmbeddingDimensionMismatch):
        make(rec2, spec=spec).embed_query("a")


@pytest.mark.parametrize("drop", [1, 2])
def test_too_few_vectors_is_an_error(drop: int) -> None:
    def respond(request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
        body = embedding_response(payload["input"])
        body["data"] = body["data"][:-drop]
        return json_response(body)

    with pytest.raises(EmbeddingResponseInvalid, match="vectors for"):
        make(Recorder(respond)).embed_documents(["a", "b", "c"])


def test_too_many_vectors_is_an_error() -> None:
    def respond(request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
        return json_response(embedding_response([*payload["input"], "extra"]))

    with pytest.raises(EmbeddingResponseInvalid):
        make(Recorder(respond)).embed_documents(["a"])


@pytest.mark.parametrize("body", [{"data": []}, {"data": None}, {}, {"data": "x"}, []])
def test_empty_or_malformed_envelope_is_an_error(body: Any) -> None:
    with pytest.raises(EmbeddingResponseInvalid):
        make(Recorder(lambda r, p: json_response(body))).embed_documents(["a"])


def _raw_embedding(values: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=f'{{"data":[{{"index":0,"embedding":[{values}]}}]}}'.encode(),
        headers={"content-type": "application/json"},
    )


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_values_are_rejected(bad: str) -> None:
    spec = dataclasses.replace(DEFAULT_SPEC, dimensions=3)
    rec = Recorder(lambda r, p: _raw_embedding(f"0.1,{bad},0.3"))
    with pytest.raises(EmbeddingResponseInvalid, match="non-finite"):
        make(rec, spec=spec).embed_query("a")


@pytest.mark.parametrize("bad", ["null", '"0.5"', "true", "[1]", "{}"])
def test_non_numeric_values_are_rejected(bad: str) -> None:
    spec = dataclasses.replace(DEFAULT_SPEC, dimensions=3)
    rec = Recorder(lambda r, p: _raw_embedding(f"0.1,{bad},0.3"))
    with pytest.raises(EmbeddingResponseInvalid, match="non-numeric"):
        make(rec, spec=spec).embed_query("a")


def test_embedding_that_is_not_a_list_is_rejected() -> None:
    rec = Recorder(lambda r, p: json_response({"data": [{"index": 0, "embedding": "AAAA"}]}))
    with pytest.raises(EmbeddingResponseInvalid, match="not a list"):
        make(rec).embed_query("a")


def test_normalisation_makes_unit_vectors() -> None:
    spec = dataclasses.replace(DEFAULT_SPEC, dimensions=3)
    rec = Recorder(lambda r, p: _raw_embedding("3,4,0"))
    assert close(make(rec, spec=spec).embed_query("a"), [0.6, 0.8, 0.0])


def test_without_normalisation_values_pass_through() -> None:
    spec = dataclasses.replace(DEFAULT_SPEC, dimensions=3, normalize=False)
    rec = Recorder(lambda r, p: _raw_embedding("3,4,0"))
    assert make(rec, spec=spec).embed_query("a") == [3.0, 4.0, 0.0]


def test_zero_vector_is_rejected_when_normalising() -> None:
    spec = dataclasses.replace(DEFAULT_SPEC, dimensions=3)
    rec = Recorder(lambda r, p: _raw_embedding("0,0,0"))
    with pytest.raises(EmbeddingResponseInvalid, match="norm"):
        make(rec, spec=spec).embed_query("a")


@pytest.mark.parametrize("echoed", ["some/other-model", "google/embedding-gemma-300m-v2", ""])
def test_wrong_model_echo_is_an_error(echoed: str) -> None:
    rec = Recorder(lambda r, p: json_response(embedding_response(p["input"], model=echoed)))
    with pytest.raises(EmbeddingModelMismatch) as info:
        make(rec).embed_query("a")
    assert info.value.transient is False
    assert len(rec.requests) == 1


def test_missing_or_non_string_model_echo_is_accepted() -> None:
    rec = Recorder(lambda r, p: json_response(embedding_response(p["input"], model=None)))
    assert len(make(rec).embed_query("a")) == DIM


def test_indexes_default_to_position_when_absent() -> None:
    def respond(request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
        body = embedding_response(payload["input"])
        for item in body["data"]:
            del item["index"]
        return json_response(body)

    texts = ["one", "two"]
    vectors = make(Recorder(respond)).embed_documents(texts)
    assert close(vectors[1], hash_embed(DOC + "two", DIM))


@pytest.mark.parametrize("indexes", [[0, 0], [1, 2], [0, 2], [-1, 0], [0, True], [0, "1"]])
def test_bad_indexes_are_rejected(indexes: list[Any]) -> None:
    def respond(request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
        body = embedding_response(payload["input"])
        for item, index in zip(body["data"], indexes, strict=True):
            item["index"] = index
        return json_response(body)

    with pytest.raises(EmbeddingResponseInvalid):
        make(Recorder(respond)).embed_documents(["a", "b"])


# ---------------------------------------------------------------- HTTP classification and retries


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_permanent_4xx_is_rejected_without_retry(status: int) -> None:
    rec = status_response(status)
    sleeps: list[float] = []
    with pytest.raises(EmbeddingRequestRejected) as info:
        make(rec, sleeps).embed_documents(["a"])
    assert info.value.status_code == status
    assert info.value.transient is False
    assert len(rec.requests) == 1
    assert sleeps == []


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_statuses_are_retried_then_unavailable(status: int) -> None:
    rec = status_response(status)
    sleeps: list[float] = []
    with pytest.raises(EmbeddingUnavailable) as info:
        make(rec, sleeps).embed_documents(["a"])
    assert info.value.transient is True
    assert info.value.attempts == 3
    assert f"HTTP {status}" in str(info.value)
    assert len(rec.requests) == 3
    assert len(sleeps) == 2  # no sleep after the last attempt


def test_redirects_are_not_followed_and_are_permanent() -> None:
    rec = Recorder(lambda r, p: httpx.Response(307, headers={"location": "http://evil.example/x"}))
    with pytest.raises(EmbeddingRequestRejected) as info:
        make(rec, api_key="sk-secret").embed_query("a")
    assert info.value.status_code == 307
    assert len(rec.requests) == 1


def test_recovers_after_transient_failures() -> None:
    def respond(request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
        if len(rec.requests) < 3:
            return httpx.Response(503)
        return json_response(embedding_response(payload["input"]))

    rec = Recorder(respond)

    sleeps: list[float] = []
    assert len(make(rec, sleeps).embed_documents(["a"])) == 1
    assert len(rec.requests) == 3
    assert len(sleeps) == 2


def test_backoff_is_exponential_with_jitter_and_injectable() -> None:
    rec = status_response(503)
    sleeps: list[float] = []
    with pytest.raises(EmbeddingUnavailable):
        make(rec, sleeps, jitter=lambda: 1.0).embed_query("a")
    assert sleeps == [0.5, 1.0]
    low: list[float] = []
    with pytest.raises(EmbeddingUnavailable):
        make(status_response(503), low, jitter=lambda: 0.0).embed_query("a")
    assert low == [0.25, 0.5]


def test_backoff_is_capped() -> None:
    sleeps: list[float] = []
    with pytest.raises(EmbeddingUnavailable):
        make(status_response(503), sleeps, max_attempts=10).embed_query("a")
    assert len(sleeps) == 9
    assert max(sleeps) == 8.0
    assert sleeps == sorted(sleeps)


def test_max_attempts_one_means_no_retry() -> None:
    rec = status_response(503)
    sleeps: list[float] = []
    with pytest.raises(EmbeddingUnavailable):
        make(rec, sleeps, max_attempts=1).embed_query("a")
    assert len(rec.requests) == 1
    assert sleeps == []


def test_timeouts_are_transient() -> None:
    def respond(request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    rec = Recorder(respond)

    sleeps: list[float] = []
    with pytest.raises(EmbeddingUnavailable) as info:
        make(rec, sleeps).embed_query("a")
    assert info.value.transient is True
    assert info.value.reason == "timeout"
    assert len(rec.requests) == 3


def test_connection_errors_are_transient() -> None:
    def respond(request: httpx.Request, payload: dict[str, Any]) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(EmbeddingUnavailable) as info:
        make(Recorder(respond)).embed_query("a")
    assert "ConnectError" in info.value.reason


def test_validation_failures_are_never_retried() -> None:
    rec = Recorder(lambda r, p: json_response({"data": []}))
    with pytest.raises(EmbeddingResponseInvalid):
        make(rec).embed_query("a")
    assert len(rec.requests) == 1


@pytest.mark.parametrize("content", [b"not json", b"", b"{", b"\xff\xfe"])
def test_malformed_json_is_a_permanent_error(content: bytes) -> None:
    rec = Recorder(lambda r, p: httpx.Response(200, content=content))
    with pytest.raises(EmbeddingResponseInvalid, match="JSON") as info:
        make(rec).embed_query("a")
    assert info.value.transient is False
    assert len(rec.requests) == 1


def test_every_error_exposes_transient() -> None:
    for error_type in (
        EmbeddingUnavailable,
        EmbeddingRequestRejected,
        EmbeddingResponseInvalid,
        EmbeddingDimensionMismatch,
        EmbeddingModelMismatch,
        EmbeddingInputInvalid,
        EmbeddingConfigurationError,
    ):
        assert issubclass(error_type, EmbeddingError)
        assert isinstance(error_type.transient, bool)
    assert EmbeddingUnavailable.transient is True
    assert all(
        not t.transient
        for t in (
            EmbeddingRequestRejected,
            EmbeddingResponseInvalid,
            EmbeddingDimensionMismatch,
            EmbeddingModelMismatch,
            EmbeddingInputInvalid,
            EmbeddingConfigurationError,
        )
    )


# ---------------------------------------------------------------- input handling


@pytest.mark.parametrize("blank", ["", " ", "\n\t  "])
def test_blank_text_is_rejected_before_any_call(blank: str) -> None:
    rec = Recorder(ok)
    client = make(rec)
    with pytest.raises(EmbeddingInputInvalid, match="blank"):
        client.embed_documents(["fine", blank])
    with pytest.raises(EmbeddingInputInvalid):
        client.embed_query(blank)
    assert rec.requests == []


def test_non_string_input_is_rejected() -> None:
    with pytest.raises(EmbeddingInputInvalid):
        make(Recorder(ok)).embed_documents(["a", 3])  # type: ignore[list-item]


def test_unicode_goes_through_unchanged() -> None:
    texts = ["Café 東京 \U0001f600 مرحبا", "é (combining)"]
    rec = Recorder(ok)
    vectors = make(rec).embed_documents(texts)
    assert rec.sent_inputs == [[DOC + t for t in texts]]
    assert rec.requests[0].headers["content-type"] == "application/json"
    assert json.loads(rec.requests[0].content)["input"][0].endswith("\U0001f600 مرحبا")
    assert len(vectors) == 2


# ---------------------------------------------------------------- usage


def test_usage_is_estimated_client_side() -> None:
    rec = Recorder(ok)  # the response says prompt_tokens: 0, like LM Studio
    client = make(rec)
    result = client.embed_documents_detailed(["a" * 7, "b" * 8])
    sent = [len(DOC) + 7, len(DOC) + 8]
    assert result.usage.estimated_tokens == sum(-(-n // 4) for n in sent)
    assert result.usage.estimated_tokens > 0
    assert (result.usage.requests, result.usage.inputs) == (1, 2)
    client.embed_query("q" * 4)
    assert client.usage.requests == 2
    assert client.usage.inputs == 3
    assert client.usage.estimated_tokens == result.usage.estimated_tokens + -(
        -(len(QUERY) + 4) // 4
    )


# ---------------------------------------------------------------- secrets and logs


def test_secrets_never_appear_in_errors_logs_or_repr(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secret_text = "TOPSECRET-DOCUMENT-TEXT"
    rec = Recorder(lambda r, p: httpx.Response(503, text=f"echo {secret_text} sk-live-key"))
    client = make(rec, api_key="sk-live-key", base_url="http://embedhost:1234/v1")
    with pytest.raises(EmbeddingUnavailable) as info:
        client.embed_documents([secret_text])
    own_logs = " ".join(r.getMessage() for r in caplog.records if r.name == "ukos.ai.embeddings")
    rendered = " ".join([str(info.value), repr(info.value), repr(client), own_logs])
    for needle in (secret_text, "sk-live-key", "embedhost", "1234"):
        assert needle not in rendered
    # httpx's own INFO line may show the (credential-free, validated) endpoint URL: never secrets.
    for needle in (secret_text, "sk-live-key"):
        assert needle not in caplog.text
    assert own_logs  # the retries were logged, safely
    assert rec.requests[0].headers["authorization"] == "Bearer sk-live-key"


def test_errors_from_the_server_body_are_not_echoed() -> None:
    rec = Recorder(lambda r, p: httpx.Response(400, text="TOPSECRET body with the input text"))
    with pytest.raises(EmbeddingRequestRejected) as info:
        make(rec).embed_query("a")
    assert "TOPSECRET" not in str(info.value)


def test_validation_errors_do_not_contain_vectors_or_texts() -> None:
    rec = Recorder(lambda r, p: json_response(embedding_response(p["input"], dimensions=DIM - 1)))
    with pytest.raises(EmbeddingDimensionMismatch) as info:
        make(rec).embed_query("TOPSECRET-QUERY")
    assert "TOPSECRET" not in str(info.value)


def test_no_authorization_header_without_a_key() -> None:
    rec = Recorder(ok)
    make(rec).embed_query("a")
    assert "authorization" not in rec.requests[0].headers


# ---------------------------------------------------------------- external-provider guard


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "127.0.0.1",
        "127.5.5.5",
        "::1",
        "[::1]",
        "host.docker.internal",
        "lmstudio",
        "embeddings.localhost",
        "a.b.localhost",
    ],
)
def test_local_hosts_are_allowed(host: str) -> None:
    assert is_local_host(host)
    url = f"http://[{host.strip('[]')}]:1/v1" if ":" in host else f"http://{host}:1234/v1"
    make(Recorder(ok), base_url=url)


@pytest.mark.parametrize(
    "host",
    ["api.openai.com", "example.com", "10.0.0.5", "192.168.1.10", "evil.localhost.example.com", ""],
)
def test_external_hosts_are_refused_by_default(host: str) -> None:
    assert not is_local_host(host)
    if host:
        with pytest.raises(EmbeddingConfigurationError) as info:
            make(Recorder(ok), base_url=f"https://{host}/v1")
        assert host not in str(info.value)
        assert info.value.transient is False


def test_external_hosts_need_the_explicit_setting() -> None:
    client = make(Recorder(ok), base_url="https://api.example.com/v1", allow_external=True)
    assert "example.com" not in repr(client)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://localhost/v1",
        "localhost:1234/v1",
        "http://user:pass@localhost:1234/v1",
        "http://localhost:1234/v1?key=abc",
        "http://localhost:1234/v1#frag",
        "",
    ],
)
def test_malformed_or_credentialed_urls_are_refused(url: str) -> None:
    with pytest.raises(EmbeddingConfigurationError) as info:
        make(Recorder(ok), base_url=url, allow_external=True)
    assert "pass" not in str(info.value)
    assert "abc" not in str(info.value)


@pytest.mark.parametrize("kwargs", [{"batch_size": 0}, {"max_attempts": 0}])
def test_bad_client_parameters_are_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(EmbeddingConfigurationError):
        make(Recorder(ok), **kwargs)


# ---------------------------------------------------------------- lifecycle and construction


def test_context_manager_closes_the_http_client() -> None:
    with make(Recorder(ok)) as client:
        client.embed_query("a")
    assert client._http.is_closed


def test_from_settings_applies_every_setting() -> None:
    settings = EmbeddingSettings(
        AI_BASE_URL="http://lmstudio:1234/v1",
        AI_API_KEY="sk-1",  # type: ignore[arg-type]
        INGEST_EMBED_BATCH_SIZE=2,
        AI_EMBEDDING_MAX_ATTEMPTS=2,
    )
    rec = Recorder(ok)
    sleeps: list[float] = []
    client = EmbeddingClient.from_settings(settings, sleeper=sleeps.append, transport=rec.transport)
    client.embed_documents(["a", "b", "c"])
    assert [len(b) for b in rec.sent_inputs] == [2, 1]
    assert rec.requests[0].headers["authorization"] == "Bearer sk-1"
    assert str(rec.requests[0].url) == "http://lmstudio:1234/v1/embeddings"
    assert client.spec == DEFAULT_SPEC


def test_build_embedding_client_reads_the_environment_and_hides_pydantic_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_BASE_URL", "http://localhost:1/v1")
    monkeypatch.setenv("AI_EMBEDDING_DIMENSIONS", "16")
    with build_embedding_client() as client:
        assert client.spec.dimensions == 16
    monkeypatch.setenv("AI_EMBEDDING_DIMENSIONS", "not-a-number")
    with pytest.raises(EmbeddingConfigurationError, match="AI_EMBEDDING_DIMENSIONS") as info:
        build_embedding_client()
    assert "not-a-number" not in str(info.value)
