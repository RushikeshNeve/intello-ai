# Remaining plan (M04 close → M08), lean mode (see memory `token-budget`, `docs/execution/TEST_DEBT.md`)

Rules: ALL tasks built; sanity tests only; one slim contract per milestone (≤ 100 lines, coordinator); 3 agents per milestone (backend A, backend B, web); one smoke gate per milestone; briefs = 10-line task + `docs/development/agent-rules.md`; handoffs ≤ 25 lines; push after each gate.

| Step | Scope | Agents |
|---|---|---|
| M04 close | merge chat runtime + web; smoke gate (stub model, then one real LM Studio run); trackers; push | 0 new (2 running) |
| M05 graph (11) | A: ontology + extraction + resolution + claim/evidence edges (M05.01.*); B: permission-safe traversal + selective router + evidence merge + bounded neighborhood API + hybrid-vs-graph benchmark (M05.02.*, .03.01, .03.04); W: React Flow explorer + edge provenance (M05.03.02-.03) | 3 |
| M06 connectors/formats (11) | A: connector port, cursor, token encryption, scheduler/webhook, first connector, revocation (M06.01.01,.03-.06); B: safe URL capture + CSV/XLSX + repo locators + OCR/audio/video contracts (M06.01.02, M06.02.*); W: only if needed (connector UI inside B) | 2-3 |
| M07 memory/research/UX (11) | A: memory model, APIs, invalidation (M07.01.01-.03); B: research sessions, planner, reports (M07.02.01-.03); W: memory UI, research workspace, command palette, code/sheet viewers, light/dark (M07.01.04, M07.02.04, M07.03.*) | 3 |
| M08 hardening/release (12) | A: lean adversarial + deletion-lineage + credential tests (M08.01.*); B: backup automation + restore rehearsal + tracing + minimal load test (M08.02.*); C: CI gates, install/security runbooks, acceptance checklist, tag baseline, deferrals (M08.03.*) | 3 |

Dependencies that are only formal (not technical): M06.01/.02 do not need the graph, so M06 batch A can start while M05 UI is finishing. Decisions needed from the user: first connector (see below) and its test credentials by M06.

## M09 — UI/UX revamp (user decision 2026-11-19)
Not part of the original M00–M08 plan. Build ALL functionality first (M04–M08, current UI stays functional-but-plain); then a dedicated milestone M09 reworks the whole UI/UX following `docs/ui-spec/PRODUCT_UI_SPEC.md`, `docs/ui-spec/DESIGN_TOKENS.md` and `docs/ui-spec/CLAUDE_FRONTEND_HANDOFF.md` (user-supplied; read them at M09 start, write a slim `m09-contracts.md`, then republish the UI). Until then: no UX polish work, only functional UI.

## Release policy and M10 (user decisions 2026-11-20)
- **M08 is NOT a release**: lean hardening + a git baseline tag only (no deployment, no publication).
- **Nothing is released before M09 (UI revamp).** After M09: **M10 — Production deployment** on free sources (backend first), then decide the public release.
- Planning only until the user approves a target and any account/cost (cloud use requires explicit approval; credentials only via the user's own secret stores, never chat).

### M10 outline (what must be solved; free-tier realities to verify at M10 start)
| Area | Local today | Production need | Free-tier fit (verify) |
|---|---|---|---|
| API + worker + dispatcher | 3 containers | 2 long-running processes + web service | Render free: web services only (sleep after ~15 min idle), NO free background workers → worker/dispatcher need another host or run in-process |
| Web (Next.js) | container | static/SSR host | Vercel / Cloudflare Pages / same host |
| PostgreSQL (RLS, definer functions, roles `ukos_app`/`ukos_worker`) | container | managed Postgres that allows roles + SECURITY DEFINER owner bypassing RLS | Neon/Supabase free (500 MB, may pause); Render free Postgres expires after ~30 days; must verify owner role can bypass RLS |
| Redis (Celery broker only) | container | small broker | Render Key Value free (25 MB, no persistence) OK; Upstash free command limits too low for Celery polling |
| Object store (S3) | RustFS | S3-compatible | Cloudflare R2 / Backblaze B2 free ~10 GB (config only) |
| RushDB (vector + keyword + graph) | container | persistent disk + ≥1 GB RAM | no true free managed tier known: self-host on a VM, or RushDB Cloud free plan if available (verify) |
| Identity (Dex) | container | OIDC provider | keep Dex on the VM, or swap to a hosted OIDC (config only) |
| LLM + embeddings | LM Studio on the user's PC | hosted inference reachable from the cloud | none free at scale: OpenAI/other under tenant policy (key via secret store) or a free-tier API; embeddings = NEW embedding space → re-embed |
| Web session store | in-memory | shared store | move sessions to Redis before >1 web instance |

**Recommended path (to confirm with the user):** one always-free VM (e.g. Oracle Cloud Always Free ARM, up to 24 GB RAM) running the SAME Docker Compose stack incl. RushDB/Postgres/Redis/object store/Dex/worker, Caddy/TLS in front, web on the same VM or Vercel; Render then only as a front for stateless pieces if wanted. Alternative: split across Render (API/web) + Neon + R2 + external VM for worker/RushDB.
M10 tasks: (1) target decision + cost/limit sheet, (2) prod config hygiene (no host.docker.internal; env-driven; secrets), (3) prod images + healthchecks, (4) data-service mapping + migrations runner, (5) inference provider + policy + re-embed plan, (6) TLS/domain/CORS/cookie + shared session store, (7) CI/CD deploy + rollback, (8) prod backups + monitoring, (9) production smoke test + free-tier limit handling (cold starts, pauses), (10) public-release checklist.
