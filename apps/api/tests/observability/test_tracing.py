"""M08.02.04: correlation propagation (header, outbox payload, Celery headers, worker context),
structured logs without content, request middleware. No database, no broker."""

from __future__ import annotations

import io
import json
import logging
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import tracing
from app.workers.dispatcher import Delivery, Dispatcher, OutboxRow

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT = "00f067aa0ba902b7"
TP = f"00-{TRACE}-{PARENT}-01"


def test_traceparent_parse_and_format() -> None:
    assert tracing.parse_traceparent(TP) == (TRACE, PARENT, "01")
    assert tracing.parse_traceparent(TP.upper()) == (TRACE, PARENT, "01")
    for bad in (
        None,
        "",
        "garbage",
        f"00-{'0' * 32}-{PARENT}-01",
        f"00-{TRACE}-{'0' * 16}-01",
        TP + "x",
    ):
        assert tracing.parse_traceparent(bad) is None
    assert tracing.format_traceparent(TRACE, PARENT) == TP
    assert tracing.clean_request_id("req-12345678") == "req-12345678"
    assert tracing.clean_request_id("bad id with spaces") is None
    assert tracing.clean_request_id("x" * 200) is None


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/v1/orgs/{org_id}/thing/{thing_id}")
    def thing(org_id: str, thing_id: str) -> dict[str, Any]:
        logging.getLogger("ukos.test").info("inside the request")
        return {
            "outbox": tracing.with_trace({"job_id": "j1"}),
            "outbound": tracing.outbound_headers(),
        }

    app.add_middleware(tracing.TraceMiddleware)
    return app


def _capture_json_logs() -> tuple[io.StringIO, logging.Handler]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(tracing.JsonFormatter("api"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    return stream, handler


def test_middleware_propagates_context_into_outbox_and_outbound_headers() -> None:
    stream, handler = _capture_json_logs()
    org = uuid.uuid4()
    try:
        client = TestClient(_app())
        res = client.get(
            f"/v1/orgs/{org}/thing/secret-doc-title?q=the+secret+query",
            headers={"traceparent": TP, "X-Request-Id": "req-abcdef12"},
        )
    finally:
        logging.getLogger().removeHandler(handler)
    assert res.status_code == 200
    assert res.headers["x-request-id"] == "req-abcdef12"
    response_trace = tracing.parse_traceparent(res.headers["traceparent"])
    assert response_trace is not None
    assert response_trace[0] == TRACE
    body = res.json()
    # outbox payload carries the trace (traceparent of THIS request's span) and the request id
    assert body["outbox"]["job_id"] == "j1"
    assert tracing.parse_traceparent(body["outbox"]["trace"]["traceparent"])[0] == TRACE  # type: ignore[index]
    assert body["outbox"]["trace"]["request_id"] == "req-abcdef12"
    # calls to the embedding/chat server: same trace and request id, a NEW span id
    out = tracing.parse_traceparent(body["outbound"]["traceparent"])
    assert out is not None
    assert out[0] == TRACE
    assert out[1] != response_trace[1]
    assert body["outbound"]["X-Request-Id"] == "req-abcdef12"
    # logs: JSON lines with trace_id/request_id/hashed org; no org id, no query text, no title
    raw = stream.getvalue()
    lines = [json.loads(line) for line in raw.splitlines()]
    lines = [line for line in lines if line["logger"].startswith("ukos")]  # not the test client's
    assert len(lines) == 2
    assert all(line["trace_id"] == TRACE for line in lines)
    assert all(line["request_id"] == "req-abcdef12" for line in lines)
    assert all(line["org"] == tracing.hash_org(str(org)) for line in lines)
    request_line = next(line for line in lines if line["msg"] == "request")
    assert request_line["route"] == "/v1/orgs/{org_id}/thing/{thing_id}"
    assert request_line["status"] == 200
    mine = " ".join(json.dumps(line) for line in lines)
    for forbidden in (str(org), "secret-doc-title", "secret+query", "the secret query"):
        assert forbidden not in mine


def test_middleware_without_headers_mints_ids_and_without_context_nothing_is_added() -> None:
    assert tracing.with_trace({"job_id": "j1"}) == {"job_id": "j1"}  # no context: payload unchanged
    assert tracing.outbound_headers() == {}
    res = TestClient(_app()).get(f"/v1/orgs/{uuid.uuid4()}/thing/x")
    assert tracing.parse_traceparent(res.headers["traceparent"]) is not None
    assert len(res.headers["x-request-id"]) >= 8
    # a malformed traceparent is ignored, never trusted
    res = TestClient(_app()).get(
        f"/v1/orgs/{uuid.uuid4()}/thing/x", headers={"traceparent": "nope"}
    )
    assert res.status_code == 200
    assert tracing.parse_traceparent(res.headers["traceparent"])


def test_json_formatter_never_logs_exception_messages() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(tracing.JsonFormatter("worker"))
    log = logging.getLogger("ukos.test.exc")
    log.addHandler(handler)
    try:
        try:
            raise ValueError("document text that must not be logged")
        except ValueError:
            log.error("task failed", exc_info=True, extra={"task": "ingest.run_job", "text": "no"})
    finally:
        log.removeHandler(handler)
    doc = json.loads(stream.getvalue())
    assert doc["exc_type"] == "ValueError"
    assert doc["task"] == "ingest.run_job"
    assert "must not be logged" not in stream.getvalue()
    assert "text" not in doc


class _Publisher:
    def __init__(self) -> None:
        self.sent: list[Delivery] = []

    def __call__(self, delivery: Delivery) -> None:
        self.sent.append(delivery)


def test_outbox_payload_to_celery_headers_to_worker_context_round_trip() -> None:
    org = uuid.uuid4()
    with tracing.bind(trace_id=TRACE, span_id=PARENT, request_id="req-abcdef12"):
        payload = tracing.with_trace({"job_id": str(uuid.uuid4())})
    # dispatcher side
    headers = tracing.celery_headers(payload, org)
    assert headers == {
        "traceparent": TP,
        "request_id": "req-abcdef12",
        "org": tracing.hash_org(org),
    }
    # worker side: the Celery request exposes message headers as attributes; the context comes back
    with tracing.bind_from_headers(headers, job_id="job-1") as ctx:
        assert ctx.trace_id == TRACE
        assert ctx.request_id == "req-abcdef12"
        assert ctx.org == tracing.hash_org(org)
        assert ctx.job_id == "job-1"
        assert ctx.span_id not in (None, PARENT)  # the task is a new span under the API span
        assert tracing.parse_traceparent(tracing.outbound_headers()["traceparent"])[0] == TRACE  # type: ignore[index]
    # a row without trace (older rows, reaper-created rows) gives no headers and a fresh trace
    assert tracing.celery_headers({"job_id": "x"}, org) == {}
    with tracing.bind_from_headers({}) as fresh:
        assert fresh.trace_id
        assert fresh.trace_id != TRACE
    # garbage in the payload is dropped, not forwarded
    assert tracing.celery_headers({"trace": {"traceparent": "x", "request_id": "no no"}}, org) == {}


def test_dispatcher_publishes_trace_headers_for_traced_rows_only() -> None:
    org, job = uuid.uuid4(), uuid.uuid4()
    pub = _Publisher()
    dispatcher = Dispatcher.__new__(Dispatcher)  # only the row -> delivery step is exercised
    dispatcher.publish = pub
    from app.workers import dispatcher as module

    traced = OutboxRow(
        id=uuid.uuid4(),
        seq=1,
        org_id=org,
        aggregate_type="ingestion_job",
        aggregate_id=job,
        event_type="ingestion.job.queued",
        payload={"job_id": str(job), "trace": {"traceparent": TP}},
        attempts=0,
    )
    plain = OutboxRow(**{**traced.__dict__, "payload": {"job_id": str(job)}})
    for row, expected in ((traced, {"traceparent": TP, "org": tracing.hash_org(org)}), (plain, {})):
        headers = tracing.celery_headers(row.payload, row.org_id)
        delivery = module.replace(module.ROUTES[row.event_type](row), headers=headers)
        dispatcher.publish(delivery)
        assert dict(pub.sent[-1].headers) == expected
        assert pub.sent[-1].args == (str(job),)


@pytest.mark.parametrize("value", ["k" * 3, "ok-value-12"])
def test_request_id_validation(value: str) -> None:
    assert (tracing.clean_request_id(value) is not None) == (len(value) >= 8)


def test_celery_message_headers_reach_the_task_through_the_real_publisher() -> None:
    """dispatcher.celery_publisher -> in-memory broker -> a real Celery worker: the task sees the
    trace headers as request attributes and binds the same trace id."""
    from celery import Celery, current_task
    from celery.contrib.testing.worker import start_worker

    from app.workers.dispatcher import celery_publisher

    app = Celery("trace-test", broker="memory://", backend="cache+memory://")
    app.conf.task_default_queue = "q"

    @app.task(name="trace.echo")
    def echo() -> list[str | None]:
        request = current_task.request
        headers = {k: getattr(request, k, None) for k in ("traceparent", "request_id", "org")}
        with tracing.bind_from_headers({k: v for k, v in headers.items() if v}) as ctx:
            return [ctx.trace_id, ctx.request_id, ctx.org]

    org = uuid.uuid4()
    trace = {"trace": {"traceparent": TP, "request_id": "req-abcdef12"}}
    headers = tracing.celery_headers(trace, org)
    with start_worker(app, perform_ping_check=False, queues=["q"], loglevel="ERROR"):
        # the publisher sends by task name, exactly like the dispatcher does
        celery_publisher(app)(Delivery("trace.echo", "q", (), headers))
        result = app.send_task("trace.echo", queue="q", headers=headers).get(timeout=30)
    assert result == [TRACE, "req-abcdef12", tracing.hash_org(org)]
