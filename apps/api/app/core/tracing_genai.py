"""GenAI spans and metrics (M13.01), OpenTelemetry GenAI semantic-convention attribute names.

Token, cost and latency come from the usage Meter events (``UsageRecorder.record`` calls
:func:`on_usage`), so nothing is counted twice. Pipeline stages (retrieve, rerank, generate,
verify) wrap their work in :func:`stage`. **Never pass prompt, answer, question, chunk or user text
here**: attributes are restricted to numbers, bools and short identifier-like strings (anything
else is dropped). Spans are off (a plain ``yield``) unless OpenTelemetry is active; metrics are
cheap counters and always on. Every public function swallows its own errors.
"""

from __future__ import annotations

import contextlib
import contextvars
import re
import time
from collections.abc import Iterator
from typing import Any, Final

from app.core import tracing

_IDENT: Final = re.compile(r"^[A-Za-z0-9._:/@+-]{1,128}$")
_KINDS: Final = frozenset({"chat", "embed", "research", "graph"})
_prompt: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "ukos_genai_prompt", default=None
)
_quota: contextvars.ContextVar[str | None] = contextvars.ContextVar("ukos_genai_quota", default=None)


def _safe(attrs: dict[str, Any]) -> dict[str, str | int | float | bool]:
    out: dict[str, str | int | float | bool] = {}
    for key, value in attrs.items():
        if isinstance(value, bool | int | float):
            out[key] = value
        elif isinstance(value, str) and _IDENT.match(value):
            out[key] = value
    return out


def _label(value: object, default: str = "other") -> str:
    text = str(value)
    return text if re.fullmatch(r"[a-z0-9_.-]{1,40}", text) else default


def set_prompt(version_id: object, sha256: str) -> None:
    """Remember the prompt version of the current call (read by the next usage event)."""
    with contextlib.suppress(Exception):
        _prompt.set((str(version_id), sha256))


def set_quota_state(state: str) -> None:
    with contextlib.suppress(Exception):
        _quota.set(state)


def _tracer() -> Any:
    from opentelemetry import trace

    return trace.get_tracer("ukos.genai")


def _parent_ctx() -> Any:
    ctx = tracing.current()
    if ctx.trace_id is None:
        return None
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    return trace.set_span_in_context(
        NonRecordingSpan(
            SpanContext(
                trace_id=int(ctx.trace_id, 16),
                span_id=int(ctx.span_id or tracing.new_span_id(), 16),
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
        )
    )


@contextlib.contextmanager
def stage(name: str, **attrs: Any) -> Iterator[None]:
    """Span ``ukos.<name>`` (retrieve | rerank | generate | verify) under the bound trace."""
    if not tracing._otel_ready:
        yield
        return
    try:
        cm = _tracer().start_as_current_span(
            f"ukos.{name}", context=_parent_ctx(), attributes=_safe(attrs)
        )
        sp = cm.__enter__()
        binder = tracing.bind(span_id=format(sp.get_span_context().span_id, "016x"))
        binder.__enter__()
    except Exception:  # noqa: BLE001
        yield
        return
    try:
        yield
    except BaseException as exc:
        with contextlib.suppress(Exception):
            binder.__exit__(type(exc), exc, exc.__traceback__)
            cm.__exit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        with contextlib.suppress(Exception):
            binder.__exit__(None, None, None)
            cm.__exit__(None, None, None)


def on_usage(
    kind: str,
    provider: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    tokens_cached: int,
    latency_ms: int,
    cost_micros: int,
) -> None:
    """Called by the usage recorder for every metered call: one GenAI span plus the metrics."""
    try:
        _metrics_for(kind, tokens_in, tokens_out, latency_ms, cost_micros, tokens_cached)
    except Exception:  # noqa: BLE001
        pass
    if not tracing._otel_ready:
        return
    try:
        attrs: dict[str, Any] = {
            "gen_ai.system": provider,
            "gen_ai.request.model": model,
            "gen_ai.operation.name": "embeddings" if kind == "embed" else "chat",
            "gen_ai.usage.input_tokens": int(tokens_in),
            "gen_ai.usage.output_tokens": int(tokens_out),
            "ukos.kind": kind,
            "ukos.cache.hit": tokens_cached > 0,
            "ukos.cost.micros": int(cost_micros),
            "ukos.latency_ms": int(latency_ms),
        }
        prompt = _prompt.get()
        if prompt:
            attrs["ukos.prompt.version_id"], attrs["ukos.prompt.sha256"] = prompt
        if _quota.get():
            attrs["ukos.quota.state"] = _quota.get()
        end = time.time_ns()
        sp = _tracer().start_span(
            "ukos.embed" if kind == "embed" else "ukos.generate",
            context=_parent_ctx(),
            attributes=_safe(attrs),
            start_time=end - max(0, int(latency_ms)) * 1_000_000,
        )
        sp.end(end)
    except Exception:  # noqa: BLE001
        pass


def _metrics_for(kind: str, tin: int, tout: int, ms: int, cost: int, cached: int) -> None:
    m = tracing.metrics()
    k = kind if kind in _KINDS else "other"
    m["ai_seconds"].labels(k, "embedding" if k == "embed" else "llm", "ok").observe(ms / 1000)
    m["ai_tokens"].labels(k, "input").inc(max(0, tin))
    m["ai_tokens"].labels(k, "output").inc(max(0, tout))
    if cost > 0:
        m["ai_cost"].inc(cost)
    if cached > 0:
        m["ai_cache_hits"].labels("provider_prompt").inc()


def record_cache_hit(cache: str) -> None:
    with contextlib.suppress(Exception):
        tracing.metrics()["ai_cache_hits"].labels(_label(cache)).inc()


def record_abstain() -> None:
    with contextlib.suppress(Exception):
        tracing.metrics()["ai_abstain"].inc()


def record_citation_invalid(count: int = 1) -> None:
    with contextlib.suppress(Exception):
        if count > 0:
            tracing.metrics()["ai_citation_invalid"].inc(count)


def record_provider_error(provider: str, code: str) -> None:
    with contextlib.suppress(Exception):
        tracing.metrics()["provider_errors"].labels(_label(provider), _label(code)).inc()
