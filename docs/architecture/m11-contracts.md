# M11 contracts

Each M11 task appends its own section.

## M11.01

Usage metering (migration 0012, `app/modules/usage/`). One `usage_events` row per model call.

- Table: `usage_events(id, org_id, workspace_id?, user_id?, kind chat|embed|research|graph, provider, model, tokens_in, tokens_out, tokens_cached, latency_ms, est_cost_micros, run_id?, created_at)`. FORCE RLS by org; app and worker roles have SELECT + INSERT only (rows immutable); `ON DELETE CASCADE` from the org. No prompt/answer text is ever stored or logged.
- Recording: `usage.wiring.Meter(sessions, org_id=..., chat_kind=...)` wraps a `ChatModel` (`.chat(m)`) or embedder (`.embedder(e)`); wrapped calls record exactly one event (also on failure or cancel). `UsageRecorder.record` swallows and logs the exception type only: metering never fails a model call.
- Wired at: chat executor (`chat`), research executor (`research`), graph extraction model factory (`graph`), ingestion embed stage and search/chat/research query embeddings (`embed`). New model call sites must wrap their model/embedder with a `Meter`; the gateway itself is unchanged.
- Tokens are client-side estimates (`ceil(chars/4)`, embeddings use the client's own estimate); `tokens_cached` is 0 until a provider reports it.
- Price: setting `USAGE_PRICES_JSON={"provider/model":{"in":n,"out":n,"cached":n}}` in micro units per 1M tokens, `provider/*` fallback; unlisted = 0.
- API: `GET /v1/orgs/{o}/usage?from=&to=&group_by=day|model|kind` (default last 30 days, max 366). Owner/admin: whole org (`scope: org`); other members: own events (`scope: self`); outsider 404; bad range or group_by 422.
