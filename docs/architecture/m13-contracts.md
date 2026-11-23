# M13 contracts: AI observability

## M13.01 GenAI traces
- `core/tracing_genai.py`; hooked in `UsageRecorder.record` (`on_usage`, same numbers as the usage event, no second counting), try/except wrapped. Spans only when `OTEL_ENABLED` (else a plain `yield`); metrics always on.
- Spans `ukos.generate|embed` (back-dated by `latency_ms`): `gen_ai.system`, `gen_ai.request.model`, `gen_ai.operation.name`, `gen_ai.usage.input_tokens|output_tokens`, `ukos.cache.hit|cost.micros|prompt.version_id|prompt.sha256|quota.state|latency_ms`. Stages: `with tracing_genai.stage("retrieve"|"rerank"|"verify", ...)`; attrs are numbers/bools/identifier-like strings only, anything else is dropped (never pass text).
- Callers set `set_prompt(version_id, sha256)` / `set_quota_state(s)` (contextvars) before the model call; `record_cache_hit(cache)`, `record_abstain()`, `record_citation_invalid(n)`, `record_provider_error(provider, code)` feed the counters.
- Metrics in `tracing.metrics()`: `ukos_ai_request_duration_seconds{kind,model_class,outcome}`, `ukos_ai_tokens_total{kind,direction}`, `ukos_ai_cost_micros_total`, `ukos_ai_cache_hits_total{cache}`, `ukos_ai_abstain_total`, `ukos_ai_citation_invalid_total`, `ukos_provider_errors_total{provider,code}`. Outcome is "ok" for metered events; failures go through `record_provider_error`.
- Not yet wired (other owners): `stage()` calls in retrieval/verification, abstain/citation/cache counters, `set_prompt`.

## M13.02/03 capture and feedback
- Capture is OFF by default. `ai_policies.capture_mode` (`off|redacted|full_admin_only`) and `capture_retention_days` (1-90, default 14); admin API `GET/PUT /v1/orgs/{o}/ai-captures/settings`.
- `ai_run_captures` (migration 0020, FORCE RLS): redacted prompt messages + answer (hard-limited 32k/16k chars), evidence `chunk_ids` and `document_ids`, `expires_at`. Written by the chat worker after a completed run (best effort, never logged); read/deleted by org owners/admins only (`GET /ai-captures`, `GET|DELETE /ai-captures/{id}`).
- Redaction: `app.modules.firewall.pii.redact` when present, else the built-in conservative regex (`capture.builtin_redact`: emails, 8+ digit runs, key-like tokens). `full_admin_only` stores unredacted.
- Lifetime: dispatcher sweep calls `ai_run_captures_sweep()` (definer) for expired rows; `deny_in_postgres` (document deletion) deletes captures whose `document_ids` contain the document, in the same transaction.
- `answer_feedback` (migration 0021): one row per (message, user), rating `up|down`, reason code only for `down` (wrong_answer, wrong_citation, missing_info, unsafe, other), no free text; RLS: own rows, owners/admins read all.
- API: `PUT|GET /v1/orgs/{o}/chat/messages/{id}/feedback` (message must be the caller's: concealed 404); `GET /v1/orgs/{o}/feedback/review-queue` (admins): down-voted run ids + chunk ids (cited chunks plus captured evidence) in golden-set review shape.
- Web: `features/chat/feedback-buttons.tsx` thumbs + reason select on assistant messages.
- Migrations chain on `0019c_shadow_runs`; coordinator re-chains. Shared additive edits: `core/models.py` (table list + imports), `main.py`, `dispatcher.py`, `knowledge/deletion.py`, `chat.py`, m04 schema test chain/grants.
