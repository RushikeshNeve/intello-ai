# M13 contracts

## M13.01 GenAI traces
- `core/tracing_genai.py`; hooked in `UsageRecorder.record` (`on_usage`, same numbers as the usage event, no second counting), try/except wrapped. Spans only when `OTEL_ENABLED` (else a plain `yield`); metrics always on.
- Spans `ukos.generate|embed` (back-dated by `latency_ms`): `gen_ai.system`, `gen_ai.request.model`, `gen_ai.operation.name`, `gen_ai.usage.input_tokens|output_tokens`, `ukos.cache.hit|cost.micros|prompt.version_id|prompt.sha256|quota.state|latency_ms`. Stages: `with tracing_genai.stage("retrieve"|"rerank"|"verify", ...)`; attrs are numbers/bools/identifier-like strings only, anything else is dropped (never pass text).
- Callers set `set_prompt(version_id, sha256)` / `set_quota_state(s)` (contextvars) before the model call; `record_cache_hit(cache)`, `record_abstain()`, `record_citation_invalid(n)`, `record_provider_error(provider, code)` feed the counters.
- Metrics in `tracing.metrics()`: `ukos_ai_request_duration_seconds{kind,model_class,outcome}`, `ukos_ai_tokens_total{kind,direction}`, `ukos_ai_cost_micros_total`, `ukos_ai_cache_hits_total{cache}`, `ukos_ai_abstain_total`, `ukos_ai_citation_invalid_total`, `ukos_provider_errors_total{provider,code}`. Outcome is "ok" for metered events; failures go through `record_provider_error`.
- Not yet wired (other owners): `stage()` calls in retrieval/verification, abstain/citation/cache counters, `set_prompt`.
