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

Each M11 task appends its own section.

## M11.01

Usage metering (migration 0012, `app/modules/usage/`). One `usage_events` row per model call.

- Table: `usage_events(id, org_id, workspace_id?, user_id?, kind chat|embed|research|graph, provider, model, tokens_in, tokens_out, tokens_cached, latency_ms, est_cost_micros, run_id?, created_at)`. FORCE RLS by org; app and worker roles have SELECT + INSERT only (rows immutable); `ON DELETE CASCADE` from the org. No prompt/answer text is ever stored or logged.
- Recording: `usage.wiring.Meter(sessions, org_id=..., chat_kind=...)` wraps a `ChatModel` (`.chat(m)`) or embedder (`.embedder(e)`); wrapped calls record exactly one event (also on failure or cancel). `UsageRecorder.record` swallows and logs the exception type only: metering never fails a model call.
- Wired at: chat executor (`chat`), research executor (`research`), graph extraction model factory (`graph`), ingestion embed stage and search/chat/research query embeddings (`embed`). New model call sites must wrap their model/embedder with a `Meter`; the gateway itself is unchanged.
- Tokens are client-side estimates (`ceil(chars/4)`, embeddings use the client's own estimate); `tokens_cached` is 0 until a provider reports it.
- Price: setting `USAGE_PRICES_JSON={"provider/model":{"in":n,"out":n,"cached":n}}` in micro units per 1M tokens, `provider/*` fallback; unlisted = 0.
- API: `GET /v1/orgs/{o}/usage?from=&to=&group_by=day|model|kind` (default last 30 days, max 366). Owner/admin: whole org (`scope: org`); other members: own events (`scope: self`); outsider 404; bad range or group_by 422.

## M11.03/04

- Embedding cache (`ai/embeddings/cache.py`, migration `0013_embedding_cache`, flag `EMBEDDING_CACHE_ENABLED`, default off): key `(org_id, embedding_space_id, sha256(NFC text))` -> float32 vector; the text is never stored. Per-org keys + FORCE RLS by org on purpose: a global cache would let identical text in two tenants share a row, leaking its existence through hits/timing. Space change (model, dims, prefixes, spec version) always misses.
- Ingestion embeds through `CachingEmbedder` (only misses reach the embedder); queries use an in-process LRU `(org, space, hash)`. Eviction: `evict_org(session, max_rows, max_age_days)` per org tenant session (no scheduler wired yet).
- Retrieval cache (`retrieval/cache.py`, hook in `service.gather`, flag `RETRIEVAL_CACHE_ENABLED`, TTL `RETRIEVAL_CACHE_TTL_SECONDS`=30): caches the fused candidates per (org, user, workspace-set hash, query hash, mode, limits, embedding space, corpus version); skipped with a reranker, never stores degraded results.
- Corpus version: per-org counter (Redis, else in-process; Redis error = bypass). Bumped by `bump_corpus` / `bump_from_session` in document finalize, document delete, grant/revoke and ingestion publish. A hit still goes through `finish` -> `revalidate`, so liveness/authorization are re-checked every time.
- Migration chain: 0013 declares `down_revision = 0011_member_preapprovals` (no 0012 in this branch); relink at merge if 0012 lands first. `embedding_cache` is registered in `ORG_SCOPED_TABLES` and the RLS catalog test.
