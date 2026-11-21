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

## M11.07 Streaming hardening
- New additive run event `stage` `{"stage":"verifying"}` (migration 0014_chat_stage_event widens `ck_chat_run_events_type`); searching/reading/writing are derived client-side from `started`/`retrieval`/`delta`. Old clients ignore it.
- Cancel: DB `chat_cancel_run` ends the run `cancelled`; the worker stops on its next fenced write or, while the model is silent, via a 1 s `run_still_running` probe. `POST /cancel` then stores the streamed deltas as an assistant message (`verification: {status:"unverified", stopped:"cancelled"}`, no citations, `[n]` markers stripped).
- Model timeout after text: run ends `completed` with the partial answer, `citations: []`, `verification.status = "unverified"`, `stopped: "timeout"` (never `grounded`). Timeout with no text stays `failed timeout`.
- `VerificationOut` gains status `unverified` and optional `stopped`. `GET /runs/{r}` returns the message for `cancelled` runs too.
- Resume: unchanged; replay is by `seq > Last-Event-ID`, client dedupes by seq (tested).

## M11.08
Budgets/quotas (migration `0015_usage_limits`, `down_revision = 0018_reindex`; coordinator re-chains). `usage_limits(org_id, scope org|user, user_id?, kind chat|embed|research|graph|all, period day|month (UTC), max_tokens?, max_requests?, action warn|block, created_by)`, FORCE RLS; app role CRUD, worker SELECT. Also index `ix_usage_events_org_user_created`.
- scope `user` with null `user_id` = per-member default (each user counted separately); with `user_id` = that user; scope `org` = org total.
- `usage/limits.py`: `check_quota` (indexed aggregate over `usage_events`, no cache: a block applies on the next call) raises `QuotaExceeded` when consumption >= max for a `block` limit; `warn` only reports (`QuotaGuard.warnings`, GET `state: warn`). Per-user burst: setting `USAGE_USER_BURST_PER_MINUTE` (default 120, 0 = off), counted from usage events of the last minute. `QuotaGuard` fails open on infra errors.
- Enforcement: `Meter(enforce_chat=True)` (set by `run_meter`: chat and research runs) checks before every `stream/complete`; `Meter(enforce_embed_query=True)` (search route) checks before `embed_query`. Ingestion/graph meters do not enforce (no redelivery storms).
- HTTP: `QuotaExceeded` -> 429 `{detail:"quota_exceeded", message, resets_at}` + `Retry-After`. Runs/research fail terminally with `error_code quota_exceeded` (`map_error`), safe message from `ERROR_MESSAGES`; web `runErrorMessage` shows it.
- API (owner/admin; member 403; outsider 404): `GET|POST /v1/orgs/{o}/usage/limits`, `PUT|DELETE .../{id}`; rows carry `consumed_tokens/requests`, `state ok|warn|blocked`, `resets_at`. Web: Limits panel with bars in Settings > Usage.
