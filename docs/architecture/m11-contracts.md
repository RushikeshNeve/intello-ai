# M11 contracts

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
