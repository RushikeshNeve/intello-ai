# Local model (LM Studio)

Intello talks to LM Studio's OpenAI-compatible server (`AI_BASE_URL`, default
`http://host.docker.internal:1234/v1`) for chat (`CHAT_MODEL`) and embeddings
(`AI_EMBEDDING_MODEL`).

## Keep the model loaded

- LM Studio loads a model on the first request and may unload it when idle. A cold load can take
  tens of seconds and makes the first chat time out-prone.
- In LM Studio (Developer > Server settings / My Models > model settings) turn off auto-unload, or
  set the idle **TTL** high (for example 24 h). Disable "JIT loading auto-evict" if you run both a
  chat and an embedding model, so loading one does not evict the other.
- **Context size**: set the loaded context length to at least `CHAT_CONTEXT_TOKENS` (default 4096).
  A smaller loaded context than the app assumes truncates prompts. Larger contexts use more RAM.
- Prefer a stable prompt prefix: the system prompt is byte-identical for every question, so
  servers that reuse the KV/prompt cache (LM Studio does for identical prefixes) skip re-reading it.

## What the warm-up does

On API startup and when a worker becomes ready, a background thread sends one chat request
(`hi`, `max_tokens=1`) and one embedding request (`hi`) to `AI_BASE_URL`. This forces both models
into memory before the first user request. It is best effort: failures are logged at info level and
never block startup.

- `WARMUP_ENABLED=true|false` overrides. Default is on when `AI_BASE_URL` points at a local host
  (localhost, 127.0.0.1, host.docker.internal) and off for remote endpoints.
- If the log shows `warm-up chat skipped`, LM Studio is not running or the model id in `CHAT_MODEL`
  does not match a downloaded model.
