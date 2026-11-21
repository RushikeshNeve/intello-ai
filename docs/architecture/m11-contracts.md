# M11 contracts

## M11.06
- `ai/structured.py`: `generate_structured(gateway, messages, schema, max_repairs=1)` -> `StructuredResult(value|None, error, repaired)`; `parse_structured(raw, schema)` for already-obtained text.
- `gateway` = anything with `complete(messages, temperature=, max_tokens=)`; passes `response_format` only if it has `supports_json_mode = True`, else the prompt must ask for JSON.
- Fences and surrounding prose stripped; outermost `{...}` validated with pydantic.
- One repair call: prior reply (truncated) plus the validator error summary; the original prompt is not re-sent verbatim beyond the message list already used.
- `StructuredError.code`: `not_json`, `schema_mismatch`, `model_unavailable`; `detail` never holds model text.
- Counters (lazy, via `core.tracing.metrics()`): `ukos_structured_{parse_failures,repaired,failed}_total`.
- Adopted in `graph/extract.py` (`GraphReply` envelope, items still ontology-validated) and `research/planner.py` (`PlanReply`); research repair only when spare model-call budget remains, repair calls are counted in usage.

## M11.02/05

- Prompt-cache prefix: `build_prompt` message 0 is the fixed `SYSTEM_PROMPT` (no ids, timestamps or
  per-org text), so it is byte-identical across questions; evidence, history and the question follow.
  Unchanged wording, `PROMPT_VERSION` stays `grounded-qa-v1`.
- `OpenAICompatibleChat.last_cached_tokens: int | None` (models/wire.py) is set after each call from
  `usage.prompt_tokens_details.cached_tokens` when the provider sends it, else `None`. The OpenAI
  adapter now sends `stream_options.include_usage` so the usage chunk arrives. Metering reads it.
- `app/modules/ai/warmup.py`: `warm_models()` (sync, never raises, returns `{"chat","embedding"}`
  booleans) and `start_warmup_background()` (daemon thread). Hooked in API lifespan and worker
  `worker_ready`. Env `WARMUP_ENABLED` (default true when `AI_BASE_URL` host is local).
