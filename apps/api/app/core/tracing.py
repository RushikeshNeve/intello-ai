"""Correlation ids, structured logs, OpenTelemetry and Prometheus metrics (M08.02.04).

One correlation context travels API -> outbox payload -> Celery headers -> worker -> embedding/chat
calls: the W3C ``traceparent`` (trace id + the span id of the sender) and an ``X-Request-Id``.

* The API middleware binds the context from the incoming ``traceparent``/``X-Request-Id``
  headers (or creates new ones) and answers with ``X-Request-Id`` and ``traceparent``.
* Code that writes an outbox row adds :func:`with_trace` (payload key ``trace``); the dispatcher
  turns it into Celery message headers (:func:`celery_headers`); the worker binds it again before
  the task body (:func:`bind_from_headers`); HTTP clients to the embedding/chat server add
  :func:`outbound_headers` (an httpx request hook).
* Logs are one JSON object per line with ``trace_id``, ``request_id``, ``org`` (a short hash,
  never the id), ``job_id``. **Content is never logged**: only the formatted message of the log
  call (which the code base already keeps free of text) and the exception TYPE, never its message
  or traceback.
* OpenTelemetry is off unless ``OTEL_ENABLED``; spans then export over OTLP/HTTP. Without it the
  same ids still correlate logs. Prometheus metrics are served by a separate listener on
  ``METRICS_PORT``
  (0 = off), which the compose files never publish on the host: internal network only. They hold
  counts and durations by route template / task name, never ids or content.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

HEADER_REQUEST_ID: Final = "X-Request-Id"
HEADER_TRACEPARENT: Final = "traceparent"
_TRACEPARENT_RE: Final = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_REQUEST_ID_RE: Final = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ukos_trace_id", default=None
)
_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("ukos_span_id", default=None)
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ukos_request_id", default=None
)
_org_hash: contextvars.ContextVar[str | None] = contextvars.ContextVar("ukos_org", default=None)
_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("ukos_job_id", default=None)


# -- ids and the W3C header ----------------------------------------------------------------------


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def parse_traceparent(value: str | None) -> tuple[str, str, str] | None:
    """``(trace_id, parent_span_id, flags)`` of a valid W3C ``traceparent``, else ``None``."""
    if not value:
        return None
    match = _TRACEPARENT_RE.match(value.strip().lower())
    if match is None:
        return None
    trace_id, span_id, flags = match.groups()
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return trace_id, span_id, flags


def format_traceparent(trace_id: str, span_id: str, flags: str = "01") -> str:
    return f"00-{trace_id}-{span_id}-{flags}"


def hash_org(org_id: object) -> str:
    """Short, stable, non-reversible tag of an organization for logs and metrics labels."""
    return hashlib.sha256(f"ukos-org:{org_id}".encode()).hexdigest()[:12]


def clean_request_id(value: str | None) -> str | None:
    return value if value and _REQUEST_ID_RE.match(value) else None


# -- the context -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Context:
    trace_id: str | None
    span_id: str | None
    request_id: str | None
    org: str | None
    job_id: str | None


def current() -> Context:
    return Context(
        _trace_id.get(), _span_id.get(), _request_id.get(), _org_hash.get(), _job_id.get()
    )


@contextlib.contextmanager
def bind(
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
    request_id: str | None = None,
    org: str | None = None,
    job_id: str | None = None,
) -> Iterator[Context]:
    """Bind correlation values for the duration of the block (None keeps the current value)."""
    pairs = [
        (_trace_id, trace_id),
        (_span_id, span_id),
        (_request_id, request_id),
        (_org_hash, org),
        (_job_id, job_id),
    ]
    tokens = [(var, var.set(val)) for var, val in pairs if val is not None]
    try:
        yield current()
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def context_from_headers(headers: Mapping[str, Any]) -> tuple[str, str | None, str]:
    """``(trace_id, parent_span_id, request_id)`` from incoming headers (case-insensitive); new ids
    are minted for whatever is missing or malformed."""
    lowered = {str(k).lower(): v for k, v in headers.items()}
    parsed = parse_traceparent(_text(lowered.get("traceparent")))
    request_id = clean_request_id(_text(lowered.get("x-request-id") or lowered.get("request_id")))
    trace_id, parent = (parsed[0], parsed[1]) if parsed else (new_trace_id(), None)
    return trace_id, parent, request_id or new_trace_id()[:16]


def _text(value: Any) -> str | None:
    if isinstance(value, bytes):
        return value.decode("ascii", "replace")
    return value if isinstance(value, str) else None


# -- propagation points ------------------------------------------------------------------------


def outbox_trace() -> dict[str, str]:
    """The ``trace`` object stored in an outbox payload (empty without a bound context)."""
    trace_id, span_id = _trace_id.get(), _span_id.get()
    if trace_id is None or span_id is None:
        return {}
    out = {"traceparent": format_traceparent(trace_id, span_id)}
    request_id = _request_id.get()
    if request_id:
        out["request_id"] = request_id
    return out


def with_trace(payload: dict[str, Any]) -> dict[str, Any]:
    """``payload`` plus ``trace`` (unchanged when there is no context, so existing payloads and
    tests that compare them are not affected)."""
    trace = outbox_trace()
    return {**payload, "trace": trace} if trace else payload


def celery_headers(payload: Mapping[str, Any], org_id: object = None) -> dict[str, str]:
    """Celery message headers for a dispatched outbox row (dispatcher side)."""
    headers: dict[str, str] = {}
    trace = payload.get("trace")
    if isinstance(trace, dict):
        if parse_traceparent(_text(trace.get("traceparent"))):
            headers["traceparent"] = str(trace["traceparent"])
        if clean_request_id(_text(trace.get("request_id"))):
            headers["request_id"] = str(trace["request_id"])
    if headers and org_id is not None:
        headers["org"] = hash_org(org_id)
    return headers


def outbound_headers() -> dict[str, str]:
    """Headers for a call to the embedding/chat server: the current span is the parent."""
    trace_id = _trace_id.get()
    if trace_id is None:
        return {}
    out = {HEADER_TRACEPARENT: format_traceparent(trace_id, _span_id.get() or new_span_id())}
    request_id = _request_id.get()
    if request_id:
        out[HEADER_REQUEST_ID] = request_id
    return out


def httpx_request_hook(request: Any) -> None:
    """httpx ``request`` event hook: adds the correlation headers."""
    for key, value in outbound_headers().items():
        request.headers[key] = value


@contextlib.contextmanager
def bind_from_headers(
    headers: Mapping[str, Any], *, job_id: str | None = None
) -> Iterator[Context]:
    """Worker side: bind the context carried by Celery headers (a fresh trace if none)."""
    lowered = {str(k).lower(): v for k, v in headers.items()}
    trace_id, _parent, request_id = context_from_headers(lowered)
    with bind(
        trace_id=trace_id,
        span_id=new_span_id(),
        request_id=request_id,
        org=_text(lowered.get("org")),
        job_id=job_id,
    ) as ctx:
        yield ctx


# -- structured logs -----------------------------------------------------------------------------

_ALLOWED_EXTRA: Final = ("stage", "error_code", "duration_ms", "status", "method", "route", "task")
_STD_ATTRS: Final = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


class JsonFormatter(logging.Formatter):
    """One JSON object per record. Never includes exception messages or tracebacks (they may carry
    content); the exception type is enough to find the code path."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        ctx = current()
        doc: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "msg": record.getMessage(),
        }
        for key, value in (
            ("trace_id", ctx.trace_id),
            ("request_id", ctx.request_id),
            ("org", ctx.org),
            ("job_id", ctx.job_id),
        ):
            if value:
                doc[key] = value
        for key in _ALLOWED_EXTRA:
            if key in record.__dict__ and key not in _STD_ATTRS:
                doc[key] = record.__dict__[key]
        if record.exc_info and record.exc_info[0] is not None:
            doc["exc_type"] = record.exc_info[0].__name__
        return json.dumps(doc, separators=(",", ":"), default=str)


_configured: set[str] = set()


def configure_logging(service: str, level: str = "info", fmt: str = "json") -> None:
    """Install the root handler once per process (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    if service in _configured:
        return
    _configured.add(service)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter(service))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    # uvicorn's access log carries paths (ids) and is replaced by the request log of the middleware.
    logging.getLogger("uvicorn.access").disabled = True


# -- OpenTelemetry (optional) --------------------------------------------------------------------

_otel_ready = False


def setup_otel(service: str, *, enabled: bool, endpoint: str) -> bool:
    """Start the SDK exporting OTLP/HTTP spans. Returns whether tracing is active."""
    global _otel_ready
    if not enabled or _otel_ready:
        return _otel_ready
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    _otel_ready = True
    return True


@contextlib.contextmanager
def span(name: str, **attributes: str | int | float | bool) -> Iterator[None]:
    """A child span of the bound context. Always moves the context to a new span id (so the
    ``traceparent`` sent downstream names this span); exports it only when OpenTelemetry is on.
    Attributes must be counts, names and ids of NOTHING content-bearing."""
    trace_id = _trace_id.get()
    parent = _span_id.get()
    if trace_id is None:
        yield
        return
    if not _otel_ready:
        with bind(span_id=new_span_id()):
            yield
        return
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    parent_ctx = trace.set_span_in_context(
        NonRecordingSpan(
            SpanContext(
                trace_id=int(trace_id, 16),
                span_id=int(parent or new_span_id(), 16),
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
        )
    )
    tracer = trace.get_tracer("ukos")
    with tracer.start_as_current_span(name, context=parent_ctx, attributes=attributes) as sp:
        sid = format(sp.get_span_context().span_id, "016x")
        with bind(span_id=sid):
            yield


# -- Prometheus (optional, internal listener) --------------------------------------------------

_metrics_lock = threading.Lock()
_metrics: dict[str, Any] = {}


def metrics() -> dict[str, Any]:
    """The process metrics (created once; prometheus_client is imported lazily)."""
    with _metrics_lock:
        if not _metrics:
            from prometheus_client import Counter, Histogram

            _metrics["http_seconds"] = Histogram(
                "ukos_http_request_duration_seconds",
                "API request latency by route template and status class",
                ["method", "route", "status"],
                buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
            )
            _metrics["task_seconds"] = Histogram(
                "ukos_task_duration_seconds",
                "Celery task duration by task name and outcome",
                ["task", "outcome"],
                buckets=(0.05, 0.25, 1, 2.5, 5, 10, 30, 60, 120, 300, 900),
            )
            _metrics["dispatched"] = Counter(
                "ukos_outbox_dispatched_total", "Outbox rows published to Celery", ["task"]
            )
            _metrics["dispatch_errors"] = Counter(
                "ukos_outbox_dispatch_errors_total", "Outbox rows that failed to publish", ["kind"]
            )
        return _metrics


def start_metrics_listener(port: int, *, queue_depth_url: str | None = None) -> bool:
    """Serve ``/metrics`` on ``port`` (all interfaces of the container, never published on the host
    by the compose files). ``queue_depth_url``: a Redis URL; adds ``ukos_queue_depth{queue}``."""
    if port <= 0:
        return False
    from prometheus_client import REGISTRY, CollectorRegistry, start_http_server
    from prometheus_client.core import GaugeMetricFamily

    metrics()
    registry: Any = REGISTRY
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    if queue_depth_url:

        class _QueueDepth:
            def collect(self) -> Iterator[Any]:
                family = GaugeMetricFamily(
                    "ukos_queue_depth", "Celery messages waiting in a queue", labels=["queue"]
                )
                try:
                    import redis

                    client = redis.Redis.from_url(str(queue_depth_url), socket_timeout=1)
                    for queue in ("ingest", "maintenance", "chat", "research"):
                        family.add_metric([queue], float(client.llen(queue)))
                except Exception:  # noqa: BLE001 - a broker blip must not break the scrape
                    return
                yield family

        registry.register(_QueueDepth())
    start_http_server(port, registry=registry)
    return True


# -- the ASGI middleware ---------------------------------------------------------------------------

_ORG_PATH_RE: Final = re.compile(r"^/v1/orgs/([0-9a-fA-F-]{36})(?:/|$)")
_request_log = logging.getLogger("ukos.request")


class TraceMiddleware:
    """Pure ASGI (SSE streams pass through untouched): binds the correlation context for the
    request, adds ``X-Request-Id`` and ``traceparent`` to the response, records the latency by
    route template and writes one log line per request (method, route template, status, duration:
    no query string, no ids)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        trace_id, _parent, request_id = context_from_headers(
            {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        )
        span_id = new_span_id()
        match = _ORG_PATH_RE.match(scope.get("path", ""))
        status_holder = {"status": 500}
        started = time.perf_counter()

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                headers.append((b"traceparent", format_traceparent(trace_id, span_id).encode()))
                message = {**message, "headers": headers}
            await send(message)

        with bind(
            trace_id=trace_id,
            span_id=span_id,
            request_id=request_id,
            org=hash_org(match.group(1).lower()) if match else None,
        ):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                route = getattr(scope.get("route"), "path", None) or "unmatched"
                status = status_holder["status"]
                elapsed = time.perf_counter() - started
                if route != "/health/live":
                    _request_log.info(
                        "request",
                        extra={
                            "method": scope.get("method"),
                            "route": route,
                            "status": status,
                            "duration_ms": round(elapsed * 1000, 1),
                        },
                    )
                if "http_seconds" in _metrics:
                    _metrics["http_seconds"].labels(
                        scope.get("method", "GET"), route, f"{status // 100}xx"
                    ).observe(elapsed)


def record_dispatch(task: str, headers: Mapping[str, Any]) -> None:
    """Dispatcher: count the publish and log it under the row's trace (when it has one)."""
    if "dispatched" in _metrics:
        _metrics["dispatched"].labels(task).inc()
    if headers:
        with bind_from_headers(headers):
            _request_log.info("dispatched", extra={"task": task})


def setup_process(settings: Any, service: str, *, queue_depth_url: str | None = None) -> None:
    """Logging, OpenTelemetry and the metrics listener of one process, from the typed settings."""
    configure_logging(service, str(settings.UKOS_LOG_LEVEL), str(settings.UKOS_LOG_FORMAT))
    setup_otel(
        service,
        enabled=bool(settings.OTEL_ENABLED),
        endpoint=str(settings.OTEL_EXPORTER_OTLP_ENDPOINT),
    )
    if settings.METRICS_PORT:
        # a prefork child or a second app instance must not crash on the already bound port
        with contextlib.suppress(OSError):
            start_metrics_listener(int(settings.METRICS_PORT), queue_depth_url=queue_depth_url)
