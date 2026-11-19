"""Streaming client tests: SSE parser edge cases, retries, errors, secrets, real HTTP server."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.modules.ai.models.errors import (
    ChatConfigurationError,
    ChatModelUnavailable,
    ChatRequestRejected,
    ChatResponseInvalid,
    ChatStreamInterrupted,
)
from app.modules.ai.models.local import LocalChatModel
from app.modules.ai.models.openai_adapter import OpenAIChatModel
from app.modules.ai.models.wire import iter_sse_data
from tests.gateway.support import (
    DONE,
    delta_event,
    drain,
    finish_event,
    settings,
    sse_body,
    sse_event,
    sse_server,
)

SECRET = "sk-wire-SECRET-987654321"
MSGS = [{"role": "user", "content": "PROMPT-TEXT-DO-NOT-LOG"}]


def make(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    attempts: int = 3,
    sleeps: list[float] | None = None,
    **env: object,
) -> LocalChatModel:
    log = sleeps if sleeps is not None else []
    return LocalChatModel.from_settings(
        settings(AI_BASE_URL="http://localhost:1234/v1", CHAT_MAX_ATTEMPTS=attempts, **env),
        sleeper=log.append,
        jitter=lambda: 0.0,
        transport=httpx.MockTransport(handler),
    )


def ok(body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    return lambda req: httpx.Response(
        200, content=body, headers={"content-type": "text/event-stream"}
    )


def run(model: LocalChatModel) -> list[str]:
    return list(model.stream(MSGS))  # type: ignore[arg-type]


# ------------------------------------------------------------------ parser


def test_deltas_in_order_and_done() -> None:
    assert run(make(ok(sse_body("Hel", "lo", " world")))) == ["Hel", "lo", " world"]


def test_request_payload_and_default_max_tokens() -> None:
    seen: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, content=sse_body("x"))

    model = make(handler)
    list(model.stream(MSGS, temperature=0.2, stop=["END"]))  # type: ignore[arg-type]
    p = seen[0]
    assert p["model"] == "google/gemma-3-4b"
    assert p["stream"] is True
    assert p["temperature"] == 0.2
    assert p["max_tokens"] == 800
    assert p["stop"] == ["END"]
    assert p["messages"] == MSGS


def test_keepalive_comments_other_fields_multiline_data_and_crlf() -> None:
    body = (
        b": keepalive\r\n\r\n"
        b"event: message\r\nid: 7\r\nretry: 1000\r\n"
        b'data: {"choices":[{"delta":{"content":"A"}}\r\n'
        b"data: ]}\r\n\r\n"  # multi-line data is joined with a newline (valid JSON whitespace)
        b": ping\n\n" + delta_event("B") + finish_event() + DONE
    )
    assert run(make(ok(body))) == ["A", "B"]


def test_no_space_after_colon_and_bom() -> None:
    body = (
        b"\xef\xbb\xbf"
        + b'data:{"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\ndata:[DONE]\n\n'
    )
    assert run(make(ok(body))) == ["hi"]


def test_byte_by_byte_with_partial_utf8_and_split_crlf() -> None:
    text = "héllo 世界 \U0001f600 שלום"
    body = sse_body(*list(text)).replace(b"\n", b"\r\n")
    chunks = [bytes([b]) for b in body]
    events = list(iter_sse_data(chunks))
    deltas = [
        json.loads(e)["choices"][0]["delta"].get("content", "") for e in events if e != "[DONE]"
    ]
    assert "".join(deltas) == text
    assert events[-1] == "[DONE]"


def test_partial_utf8_via_http_response() -> None:
    text = "café \U0001f600 end"
    body = sse_body(text)
    cut = body.index("\U0001f600".encode()) + 2  # split inside the 4-byte emoji

    def stream() -> object:
        yield body[:cut]
        yield body[cut:]

    resp = httpx.Response(200, content=stream())  # type: ignore[arg-type]
    model = make(lambda req: resp)
    assert "".join(run(model)) == text


def test_invalid_utf8_is_invalid_response() -> None:
    body = b'data: {"choices":[{"delta":{"content":"\xff"}}]}\n\n'
    with pytest.raises(ChatResponseInvalid):
        run(make(ok(body)))


def test_reasoning_content_ignored_and_empty_deltas_skipped() -> None:
    body = (
        sse_event({"choices": [{"delta": {"reasoning_content": "thinking", "content": ""}}]})
        + sse_event({"choices": [{"delta": {"role": "assistant"}}]})
        + sse_event({"choices": []})
        + delta_event("answer")
        + finish_event("length")
        + sse_event({"usage": {"prompt_tokens": 1}})
        + DONE
    )
    model = make(ok(body))
    assert run(model) == ["answer"]
    assert model.last_finish_reason == "length"


def test_finish_without_done_is_ok_but_neither_is_interrupted() -> None:
    assert run(make(ok(sse_body("a", done=False)))) == ["a"]
    with pytest.raises(ChatStreamInterrupted):
        run(make(ok(sse_body("a", done=False, finish=False)), attempts=1))


def test_malformed_event_is_invalid_not_retried() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"data: {not json}\n\n")

    with pytest.raises(ChatResponseInvalid):
        run(make(handler))
    assert calls["n"] == 1


def test_complete_joins() -> None:
    assert make(ok(sse_body("a", "b"))).complete(MSGS) == "ab"  # type: ignore[arg-type]


# ------------------------------------------------------------------ errors and retries


def test_retry_503_then_success_with_backoff() -> None:
    answers = [
        httpx.Response(503),
        httpx.Response(429),
        httpx.Response(200, content=sse_body("ok")),
    ]
    sleeps: list[float] = []
    model = make(lambda req: answers.pop(0), sleeps=sleeps)
    assert run(model) == ["ok"]
    assert sleeps == [0.25, 0.5]


def test_retries_exhausted_is_transient_unavailable() -> None:
    with pytest.raises(ChatModelUnavailable) as err:
        run(make(lambda req: httpx.Response(500, text=f"boom {SECRET}"), attempts=2))
    assert err.value.transient
    assert err.value.attempts == 2
    assert SECRET not in str(err.value)


def test_4xx_rejected_not_retried() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad")

    with pytest.raises(ChatRequestRejected) as err:
        run(make(handler))
    assert err.value.status_code == 400
    assert not err.value.transient
    assert calls["n"] == 1


def test_timeout_and_connection_error_are_retried() -> None:
    seq: list[Exception | httpx.Response] = [
        httpx.ReadTimeout("t"),
        httpx.ConnectError("c"),
        httpx.Response(200, content=sse_body("x")),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    assert run(make(handler)) == ["x"]


def test_stream_dying_before_first_token_is_retried() -> None:
    bodies = [b": nothing\n\n", sse_body("fine")]
    assert run(make(lambda req: httpx.Response(200, content=bodies.pop(0)))) == ["fine"]


def test_mid_stream_error_after_token_is_not_retried() -> None:
    calls = {"n": 0}
    body = delta_event("part") + sse_event({"error": {"message": f"oops {SECRET}"}})

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=body)

    model = make(handler)
    got: list[str] = []
    with pytest.raises(ChatStreamInterrupted) as err:
        drain(model.stream(MSGS), got)  # type: ignore[arg-type]
    assert got == ["part"]
    assert calls["n"] == 1
    assert err.value.transient
    assert SECRET not in str(err.value)


def test_transport_failure_after_token_is_interrupted_not_retried() -> None:
    class Dying(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            yield delta_event("one")
            raise httpx.ReadError("cut")

    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, stream=Dying())

    got: list[str] = []
    with pytest.raises(ChatStreamInterrupted):
        drain(make(handler).stream(MSGS), got)  # type: ignore[arg-type]
    assert got == ["one"]
    assert calls["n"] == 1


def test_generation_deadline() -> None:
    now = {"t": 0.0}

    def clock() -> float:
        now["t"] += 100.0
        return now["t"]

    model = LocalChatModel.from_settings(
        settings(AI_BASE_URL="http://localhost:1234/v1", CHAT_GENERATION_TIMEOUT_SECONDS=50),
        transport=httpx.MockTransport(ok(sse_body("a", "b", "c"))),
    )
    model._clock = clock
    with pytest.raises(ChatStreamInterrupted) as err:
        run(model)
    assert err.value.reason == "generation timeout"


def test_response_too_long_guard() -> None:
    model = make(ok(sse_body("x" * 50, "y" * 50)))
    model._max_response_chars = 60
    with pytest.raises(ChatResponseInvalid):
        run(model)


def test_closing_the_iterator_cancels() -> None:
    it: Any = make(ok(sse_body("a", "b", "c"))).stream(MSGS)  # type: ignore[arg-type]
    assert next(it) == "a"
    it.close()  # must not raise


# ------------------------------------------------------------------ guards and secrets


def test_non_local_base_url_refused_unless_allowed() -> None:
    s = settings(AI_BASE_URL="https://api.remote.example/v1")
    with pytest.raises(ChatConfigurationError):
        LocalChatModel.from_settings(s)
    assert LocalChatModel.from_settings(s, allow_external=True) is not None
    for bad in (
        "ftp://localhost/v1",
        "http://user:pw@localhost/v1",
        "http://localhost/v1?x=1",
        "nothost",
    ):
        with pytest.raises(ChatConfigurationError):
            LocalChatModel.from_settings(settings(AI_BASE_URL=bad), allow_external=True)


def test_openai_sends_key_only_in_header_and_uses_max_completion_tokens() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=sse_body("ok"))

    model = OpenAIChatModel.from_settings(
        settings(OPENAI_API_KEY=SECRET), transport=httpx.MockTransport(handler)
    )
    assert list(model.stream(MSGS)) == ["ok"]  # type: ignore[arg-type]
    req = seen[0]
    assert str(req.url) == "https://api.openai.com/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in req.content.decode()
    assert "max_completion_tokens" in json.loads(req.content)


def test_openai_requires_key() -> None:
    with pytest.raises(ChatConfigurationError):
        OpenAIChatModel.from_settings(settings())


def test_no_secret_or_prompt_in_errors_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    model = OpenAIChatModel.from_settings(
        settings(OPENAI_API_KEY=SECRET),
        sleeper=lambda s: None,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(503, text=SECRET + "PROMPT-TEXT-DO-NOT-LOG")
        ),
    )
    with pytest.raises(ChatModelUnavailable) as err:
        list(model.stream(MSGS))  # type: ignore[arg-type]
    for text in (str(err.value), repr(err.value), caplog.text, repr(model)):
        assert SECRET not in text
        assert "PROMPT-TEXT-DO-NOT-LOG" not in text


# ------------------------------------------------------------------ real HTTP server


def real_model(url: str, **env: object) -> LocalChatModel:
    return LocalChatModel.from_settings(settings(AI_BASE_URL=url, CHAT_MAX_ATTEMPTS=1, **env))


def test_real_server_streams_incrementally_with_keepalives() -> None:
    chunks = [
        b": hello\n\n",
        delta_event("one "),
        b": ka\n\n",
        delta_event("two "),
        delta_event("é世"),
        finish_event(),
        DONE,
    ]
    with sse_server(chunks, pause=0.02) as srv:
        model = real_model(srv.base_url, AI_API_KEY="local-key-1")
        assert list(model.stream(MSGS)) == ["one ", "two ", "é世"]  # type: ignore[arg-type]
        assert srv.authorization == ["Bearer local-key-1"]
        assert srv.requests[0]["stream"] is True


def test_real_server_http_500_and_early_close() -> None:
    with sse_server([b"boom"], status=500) as srv, pytest.raises(ChatModelUnavailable):
        list(real_model(srv.base_url).stream(MSGS))  # type: ignore[arg-type]
    with sse_server([delta_event("partial")]) as srv:  # closes without finish or [DONE]
        got: list[str] = []
        with pytest.raises(ChatStreamInterrupted):
            drain(real_model(srv.base_url).stream(MSGS), got)  # type: ignore[arg-type]
        assert got == ["partial"]


def test_real_server_read_timeout() -> None:
    with sse_server([delta_event("a"), delta_event("b")], pause=0.6) as srv:
        model = real_model(srv.base_url, CHAT_TIMEOUT_SECONDS=0.2)
        got: list[str] = []
        with pytest.raises((ChatStreamInterrupted, ChatModelUnavailable)):
            drain(model.stream(MSGS), got)  # type: ignore[arg-type]
        assert got == ["a"]
