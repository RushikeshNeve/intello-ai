# Agent Handoffs

One entry per task when it reaches REVIEW or DONE. The coordinator adds verification evidence it reran itself on
`main`. Agent self-reports are kept but are not treated as proof.

---

## M00.01.01 — System boundaries and ADR index
- **Owner:** coordinator · **Branch:** main · **Status:** DONE
- **Files:** `docs/architecture/system-boundaries.md`, `tests/docs/test_system_boundaries.py`
- **Acceptance:** "Boundary table and accepted/provisional ADR references are consistent."
- **Evidence:** `uv run --no-project --with pytest pytest -q tests/docs` → `4 passed`. The checks: the register has ADR-001…057 in sequence, every ADR the doc references exists, the doc's index matches the register row-for-row (decision and status), and the core ownership rows are present.
- **Limitations:** ADR rationale documents don't exist (the register is summaries only); boundaries follow MASTER_ARCHITECTURE v1.0.

---

## M00.02.02, M00.03.01, M00.03.02, M00.03.03 — RushDB compose + PoC spikes
- **Owner:** rushdb-agent · **Branch:** `agent/m00-rushdb` (79856e1, 6721608, b45e8f5, 0ddb6fa, 38d439f) · merged to `main` as 67b0974 · **Status:** DONE
- **Files:** `infrastructure/compose/rushdb.yml`, `tests/spikes/rushdb_{support,persistence,tenants,hybrid,graph}.py`, `tests/spikes/fixtures/rushdb_hybrid_corpus.json`, `docs/spikes/rushdb-validation.md`
- **Agent self-report:** 52 passed (persistence 1, tenants 30, hybrid 13, graph 8), 0 skipped or xfail.
- **Coordinator verification (on `main` after merge):** `docker compose -p ukos-verify-rushdb --env-file infrastructure/env/.env.example -f infrastructure/compose/rushdb.yml up -d --wait` → healthy; `uv run --no-project --with pytest --with httpx python -m pytest -q tests/spikes/rushdb_persistence.py tests/spikes/rushdb_tenants.py tests/spikes/rushdb_hybrid.py tests/spikes/rushdb_graph.py` → **52 passed in 16.62s**; `down -v`. Note: this run used the env file's then-floating `rushdb/rushdb:3` tag, which resolved to the same digest as 3.0.10 on 2026-11-16. The contract is now pinned to 3.0.10.
- **Acceptance:** 02.02 restart and recreate preserve records, traversal, vector scores, BM25 and relationships ✔; 03.01 no cross-tenant leakage on any surface tried, missing or mismatched scope gets 403 ✔; 03.02 BM25, vector and RRF fusion return the exact expected fixtures, filter-before-rank holds ✔; 03.03 evidence-filtered traversal and the version purge leave no orphans ✔.
- **Verdict:** CONDITIONAL GO (see DC-009).
- **Limitations / follow-ups:** an unknown tenant scope auto-registers an empty tenant; the operator token is valid for every tenant; `query.text` can't be filtered by workspace; online base backup loses text-index content (cold snapshot recommended); per-tenant capability tokens are untested; the engine is very young (3.0.0 released 2026-10-29).
- **Contract changes applied by coordinator:** `RUSHDB_IMAGE=rushdb/rushdb:3.0.10`, `RUSHDB_HTTP_URL`, and comments on token scope and permanent multitenant mode.

---

## M00.02.03, M00.02.04 — Object storage evaluation + RustFS compose
- **Owner:** objectstore-agent · **Branch:** `agent/m00-objectstore` (c37d427, 64ee525) · merged to `main` as 55b3a70 · **Status:** DONE
- **Files:** `docs/spikes/object-storage.md`, `tests/spikes/objectstore_{contract,support,integration}.py`, `infrastructure/compose/spikes/objectstore-candidates.yml`, `infrastructure/compose/objects.yml`, `infrastructure/compose/objectstore/{init.sh,.gitattributes}`
- **Agent self-report:** contract suite 21/21 on SeaweedFS 4.53 and RustFS 1.0.5; Garage v2.5.0 had 19 pass, 1 fail and 1 unsupported. Integration 5/5. Overnight runs were discarded as invalid.
- **Coordinator verification (on `main` after merge):** `uv run --no-project --with pytest --with boto3 --with httpx python -m pytest -q -rA tests/spikes/objectstore_integration.py` → **5 passed in 61.32s**. This boots the stack, checks the buckets are private and have CORS, runs the 21-check contract suite against both buckets (21 passed each), checks persistence across `down`/`up`, and checks that re-running init is idempotent. No containers were left behind.
- **Decision:** RustFS 1.0.5 (fallback SeaweedFS 4.53). DC-002 closed.
- **Integration notes:** `up --wait` doesn't wait for `objectstore-init` to finish, so consumers use `depends_on: objectstore-init: service_completed_successfully`. The adapter (M02.01.x) uses plain S3 only: path-style SigV4, `x-amz-checksum-sha256` on originals, `If-None-Match: *` on immutable keys, and a separate presign client on `S3_PUBLIC_ENDPOINT_URL`. Providers return either 400 or 403 for an expired presigned URL, so the adapter treats any 4xx as "re-issue".
- **Backup:** for portability the primary should be a logical S3 mirror with SHA-256 verification (M08.02.01). A cold volume tar is valid only for the same provider and version; the M00.03.05 smoke uses that.
- **Contract changes applied by coordinator:** `OBJECT_STORE_IMAGE`, `OBJECT_STORE_INIT_IMAGE`, `OBJECT_STORE_LOG_LEVEL`, and a note that R2 uses region `auto`.

---

## M00.02.01 — Compose PostgreSQL + Redis
- **Owner:** platform-agent · **Commit:** f6d04a8 on `agent/m00-platform`. The coordinator merged this commit into `main` early (8383b9d), while the agent was still finishing M00.02.05 on the same branch, because M00.03.05 needs `data.yml`. **Status:** DONE
- **Files:** `infrastructure/compose/data.yml`, `infrastructure/compose/postgres/init/10-app-role.sh`, `tests/infrastructure/{compose_support,test_data_services}.py`
- **Coordinator verification (on `main`):** `UKOS_TEST_COMPOSE_PROJECT=ukos-verify-data uv run --no-project --with pytest --with "psycopg[binary]" --with redis python -m pytest -q -rA tests/infrastructure/test_data_services.py` → **5 passed in 37.92s**. The tests cover health, volumes and loopback-only ports; that secrets are not exposed in container config; that Redis requires a password and uses AOF; that the app role is least-privilege (NOSUPERUSER, NOBYPASSRLS); and persistence across restart and recreate.

---

## M00.03.05 — Backup/restore smoke
- **Owner:** coordinator · **Branch:** main · **Status:** DONE
- **Files:** `scripts/restore_smoke.sh`, `scripts/restore_smoke/fixtures.py`
- **Method:** runs in an isolated project `ukos-restore-smoke` on ports 15532/16479/13310/19300 and refuses to run against the project `ukos`. Steps: seed Postgres (250 rows), RushDB (40 chunks with vectors, a text index, HAS_CHUNK and MENTIONS links) and RustFS (12 objects from 1 KiB to about 280 KiB). Record content digests. Back up with `pg_dump -Fc` plus cold `tar` snapshots of the stopped RushDB and object-store volumes, and write a SHA-256 manifest. `down -v`, then assert the volumes are gone and verify the manifest. Restore into fresh volumes (`pg_restore`, untar), restart the stores and compare digests. RushDB's digest includes vector scores and BM25 hits, per RushDB condition (4).
- **Evidence:** `bash scripts/restore_smoke.sh` → `RESTORE SMOKE PASSED: restored data matches fixture checksums for objects, postgres, rushdb`, **exit code 0**. Restored: postgres rows=250 md5 c53b793a…; objects=12 sha256 dde726f4… (matches the generator); rushdb chunks=40, has_chunk_rels=40, mentions=8, sha256 d9d8ef50…. Nothing from `ukos-restore-smoke` was left behind afterwards.
- **Fix during verification:** the first run failed at step 2 because Git Bash passed `/d/...` paths to Windows `docker.exe` with path conversion disabled. Fixed by normalizing the repo path with `cygpath -m`.
- **Limitations:** Redis is deliberately not backed up (it's a broker, ADR-036). The object snapshot is valid only for the same provider and version; portable logical mirroring is M08.02.01. There is no point-in-time recovery: the RushDB WAL archive is untested.

---

## M00.02.05 — FastAPI + Next.js starters and apps.yml
- **Owner:** platform-agent · **Branch:** `agent/m00-platform` (49c7447) · merged to `main` as b5607d6 · **Status:** DONE
- **Files:** `apps/api/**` (uv project, `create_app()` factory, Settings, `/health/live`, `/health/ready`, Dockerfile, tests), `apps/web/**` (Next.js 16.4.6, React 19.3, Tailwind v4, shadcn/ui, `/api/health`, vitest, Dockerfile), `infrastructure/compose/apps.yml`, `tests/infrastructure/test_apps_health.py`
- **Coordinator verification (on `main`):**
  - API: `python -m ruff check` → All checks passed; `ruff format --check` → 12 files already formatted; `python -m pytest -q` → **8 passed** (1 Starlette deprecation warning); strict mypy in a `python:3.12-slim` container → **Success: no issues found in 11 source files**.
  - Web, starting from `npm ci`: lint clean; test **1/1**; build compiled, `ƒ /api/health` dynamic; typecheck **initially FAILED on a fresh checkout** (`Cannot find name 'LayoutProps'`, a type generated only by a build). Fixed: `typecheck` now runs `next typegen && tsc --noEmit`, which passes after `rm -rf .next`.
  - Integration: `UKOS_TEST_COMPOSE_PROJECT=ukos-verify-apps … python -m pytest -q tests/infrastructure/test_apps_health.py` → **6 passed in 43.43s**. All containers healthy and loopback-only; live 200, ready 200, openapi 200, web health 200; ready returns 503 when Redis is stopped. Nothing left behind.
- **Deviation accepted:** the health route uses `await connection()` instead of `export const dynamic`, because Next 16 enables `cacheComponents` by default and that combination isn't supported.
- **Host constraints:** Application Control blocks uv-managed Python and mypyc DLLs, so the venv is built from python.org Python and mypy runs in a container (runbook §7.1).
- **Follow-ups:** readiness opens a new connection per call (pooling comes later); `npm audit` reports advisories in the scaffold's dependencies, to be triaged in M08.03.01; `API_SECRET_KEY` and `API_INTERNAL_BASE_URL` are reserved and not used yet.

---

## M00.03.04 — Local embeddings and generation benchmark (LM Studio)
- **Owner:** models-agent · **Branch:** `agent/m00-models` (bdb07c7) · merged to `main` as 0e078e7 · **Status:** DONE
- **History:** first run on Ollama in Docker. At the user's direction it was stopped and redone on LM Studio (DC-011); the Ollama numbers are discarded.
- **Files:** `docs/spikes/local-models.md`, `docs/spikes/local-models-results.json`, `scripts/benchmarks/local_models.py`, `scripts/benchmarks/fixtures/{retrieval,grounded_qa}.json` (original fixtures: 50 retrieval pairs, 8 of them multilingual; 26 grounded-QA items: 16 answerable, 7 unanswerable, 3 injection)
- **Agent results:**
  - Embeddings: embedding-gemma-300m R@1 0.98 / MRR 0.985 / multilingual R@1 1.00, 768 dimensions, +396 MiB VRAM. qwen3-embedding-0.6b R@1 0.96. nomic-embed-v1.5 R@1 0.88.
  - Chat: gemma-3-4b on GPU at 57.2 tok/s, correct 0.947, citations valid 0.737, abstention 1.00, injection resisted 0.667. On CPU: 12.3 tok/s with the same accuracy.
  - gpt-oss-20b was aborted by the RAM watchdog. gemma-4-26b-a4b was skipped on its estimate (17.59 GiB against 9.4 GB available RAM).
- **Coordinator verification (on `main`):** reran the chosen pair into a scratch output (committed results untouched): `python -I scripts/benchmarks/local_models.py --label coordinator-verify --gpu max --reps 1 --embed-models google/embedding-gemma-300m --chat-models google/gemma-3-4b --restore google/gemma-3-4b --out <scratch>`. Embedding accuracy **identical** (R@1 0.98, R@5 1.00, MRR 0.985, multilingual 1.00). Grounded QA **identical** (0.947 / 0.737 / abstention 1.0 / injection 0.667 / overall 0.769). VRAM delta 4460 vs 4459 MiB. Speed 52.8 tok/s against 57.2; that's a single rep, within run-to-run noise. LM Studio was left as found (gemma-3-4b loaded, server on 1234).
- **Findings that bind later tasks:** the LM Studio licence forbids SaaS/ASP use, so it's local-only (DC-011). `/v1/embeddings` reports `prompt_tokens: 0`, so count tokens client-side. LM Studio loads models just-in-time, so allow-list models in the gateway. On 0.4.9, `--gpu off` is ignored for embedding models, so the CPU-only embedding path is unmeasured. The 3-injection set is small, and the model repeated one injected claim, so the gateway must post-validate citations (M04.02.03).
- **Contract changes applied by coordinator:** `AI_PROVIDER=lmstudio`, `AI_BASE_URL`, `AI_HOST_BASE_URL`, `AI_EMBEDDING_MODEL`, `AI_EMBEDDING_DIMENSIONS=768`, `AI_CHAT_MODEL`. Removed `OLLAMA_*`.

---

## M00.01.02 — Local env contract
- **Owner:** coordinator · **Status:** DONE
- **Files:** `infrastructure/env/.env.example`, `tests/infrastructure/test_env_contract.py`
- **Evidence:** `python -m pytest -q tests/infrastructure/test_env_contract.py` → **7 passed**. It checks that all 31 compose-interpolated variables are documented, every variable has a comment, secrets use `change-me-` placeholders, URLs embed only placeholder passwords, there are no credential-like strings, and ports bind through `UKOS_BIND_HOST`. A mutation check on a scratch copy (undocumented variable, real-looking password, 0.0.0.0 port) failed 3 tests, as expected.

## M00.01.03 — Developer runbook
- **Owner:** coordinator · **Status:** DONE
- **File:** `docs/development/local-runbook.md`
- **Evidence:** I executed §2–§5 on the full root stack: `cp .env.example .env`, `docker compose up -d --wait`, `docker compose run --rm objectstore-init`, the health checks, then `down -v`. All 6 services were healthy; ready returned postgres and redis ok; web 200; RushDB 200; anonymous object read 403; nothing left behind. The first execution caught an unreliable step (`docker compose wait objectstore-init`), which was replaced. The §4 LM Studio commands match the verified benchmark setup. The §6 test commands are the ones used for every verification above.

---

## M01.03.01 — RBAC policy decision service
- **Owner:** policy-agent · **Branch:** `agent/m01-policy` (8f26377) · merged to `main` as 72f0b72 · **Status:** DONE
- **Files:** `apps/api/app/modules/authorization/{__init__,policy}.py`, `apps/api/tests/authorization/{__init__,test_policy}.py` (standard library only)
- **Agent results:** 632 tests. The role matrix is 4 org × 4 workspace roles × 13 actions = 208 cells, each run twice (Action values and plain strings), against an independent hand-written expected table. Also covered: grants (49 cases), non-member with every grant (52 cases), unknown actions (40), and the `can_assign_org_role` matrix (64). Branch coverage of policy.py is 100% (105 statements, 28 branches). Strict mypy passes in a container.
- **Coordinator verification (on `main`):** `python -m pytest -q tests/authorization` → **632 passed**; ruff clean. The full API suite on the host with the venv under `%LOCALAPPDATA%` → **640 passed**.
- **Contract clarification:** DC-013 (the `not_workspace_member` reason code, and precedence rules).
- **Integration (for M01.02.02/04):** build `SubjectContext` from Postgres, then `decide()`. A denial maps to 404 when `is_concealed()` is true, otherwise 403. Member changes call `decide(ORG_MEMBERS_MANAGE)` and then `can_assign_org_role()`. The last-owner rule is enforced by the database trigger and the caller.

---

## M01 foundation + M01.02.01 — DB layer, models, security core, Alembic migration 0001
- **Owner:** foundation-agent (single migration owner) · **Branch:** `agent/m01-foundation` (82db3d9…04298c5) · merged to `main` as 1c1bf6a · **Status:** DONE
- **Delivered:**
  - Dependencies: SQLAlchemy 2.1, Alembic, PyJWT[crypto], and runtime httpx.
  - Settings for OIDC and the migration URL.
  - `app/core/db.py`: `tenant_session`, `set_tenant_context`, `set_identity_context`.
  - `app/core/models.py`: 7 typed models.
  - `app/core/security.py`: Principal, VerifiedClaims, the verifier/provisioner protocols, and `get_current_principal` with 401 + `WWW-Authenticate`.
  - Alembic migration `0001_identity_tenancy`: every constraint, deferred owner triggers, append-only audit, RLS ENABLE + FORCE fail-closed on all 7 tables, and least-privilege grants.
  - An `api-migrate` one-shot that `api` waits on.
  - The env overlay in `compose_support.py` for isolated test ports.
- **Coordinator verification (on `main`):**
  - ruff clean; 27 files formatted.
  - Unit tests **662 passed** (including the 632 policy tests).
  - `tests/integration/m01_foundation` → **46 passed**: migration round trip, slug and owner constraints, workspace membership FKs, append-only audit, RLS smoke as `ukos_app`.
  - Strict mypy in a container → **no issues in 26 files**.
  - `test_apps_health.py` with `api-migrate` → **6 passed**. The first attempt hit a port clash on 23301 with another agent's server; the retry on free ports passed.
  - Contract and docs tests → 11 passed.
- **M01.02.01 acceptance:** "Unique organizations and owner membership constraints pass." Duplicate and case-variant slugs are rejected; an org with no owner fails at commit (23514 / `organization_has_owner`); demoting or removing the last owner is rejected; an ownership transfer inside one transaction is allowed.
- **Contract updates accepted:** DC-014.

---

## M01.03.03 — Document grant model contracts
- **Owner:** grants-agent · **Branch:** `agent/m01-grants` (824ef84) · merged to `main` as 522a195 · **Status:** DONE
- **Files:** `apps/api/app/modules/authorization/grants.py`, `apps/api/tests/authorization/test_grants.py` (31 tests), `tests/integration/m01_grants/**` (21 DB tests)
- **Coordinator verification (on `main`):** ruff clean; unit tests **693 passed**; `tests/integration/m01_grants` + `m01_foundation` on isolated ports → **67 passed**; nothing left behind.
- **Acceptance (grant, revoke and expiry semantics):** grants are active once made, implied permissions apply, and grants never authorize outside their org. Revoke is idempotent and keeps the original revocation record. Past or naive expiry times are rejected, and a live grant becomes inactive at expiry without any update. Duplicate grants are idempotent, including under concurrency. Grants work end to end with `policy.decide`. These tests ran on a real Postgres as the app role.
- **Decisions:** DC-015.

---

## M01 batch 2 + web session — coordinator integration and verification (2026-11-16)
Merged into `main`: `agent/m01-audit` (fe05afd), `agent/m01-web-session` (521c3a1), `agent/m01-rls` (552858b), `agent/m01-identity` (de588a8).

**Coordinator fixes during integration:**
- `app/core/db.py`: `set_tenant_context` now clears the identity context. This fixes the gap the RLS suite found (DC-016).
- Migration **0002_rls_hardening**: no forged audit actor; `pg_temp` last on the SECURITY DEFINER search path; `users.issuer`/`subject` immutable. Five regression tests added in `test_09_hardening.py`.
- **Dex pinned to v2.45.1**, because v2.46.x hangs on refresh (DC-016).
- `test_security.py` updated for the wired identity.
- Two stale assertions updated: the migration head is now read from Alembic, and the identity rename is allowed to be denied by either the trigger or RLS.
- Web env (`WEB_SESSION_SECRET`, `WEB_PUBLIC_URL`, OIDC_*) added to the contract and `apps.yml` (DC-017).

**Final verification on `main`** (each suite isolated on its own project and ports; nothing left behind):

| Suite | Result |
|---|---|
| API ruff | clean |
| API unit | **820 passed** |
| strict mypy (container), app + tests + migrations | **no issues, 41 files** |
| `tests/integration/m01_foundation` | **46 passed** |
| `tests/integration/m01_grants` | **21 passed** |
| `tests/integration/m01_audit` | **15 passed** |
| `tests/integration/m01_identity` (real Dex v2.45.1) | **17 passed** |
| `tests/security/postgres_rls` | **324 passed** |
| `tests/infrastructure/test_apps_health.py` (api-migrate + new web env) | **6 passed** |
| web lint / typecheck / unit / build | clean / clean / **67 passed** / ok |
| `tests/e2e/web_auth` with `E2E_REQUIRE_REFRESH=1` | **12/12**, "provider refreshed the token server-side" |

**Acceptance verdicts:**
- M01.01.01: invalid issuer, audience and expired tokens rejected, both forged and real Dex ✔
- M01.01.02: login/logout and the HttpOnly cookie flow tested end to end ✔
- M01.01.03: idempotent user creation, including a 10-thread race producing 1 user and 1 audit row ✔
- M01.03.02: cross-organization SQL access denied (324 adversarial tests) ✔
- M01.03.04: membership and role changes create redacted audit records ✔

**Open follow-ups:**
- The web session store is in-memory, which is fine for a single process; a shared store is needed before scaling out.
- Repo-level `tests/` aren't covered by the apps/api ruff per-file ignores (S101). This needs a repo-level ruff config in M08.03.01.
- Optional org-membership checks in RLS policies are deferred to M08 (DC-016 item 4).

---

## M01 full-stack e2e harness (coordinator, 2026-11-18)
- **Files:** `tests/e2e/fullstack_m01/run.sh`, `tests/e2e/fullstack_m01/fullstack.test.mjs`
- **What it does:** starts the real stack (PostgreSQL, Redis, Dex, API with `api-migrate`, web) as project `ukos-e2e-m01` (web 33400, API 38400, Dex 38480), then plays alice / bob / mallory over HTTP like a browser through the BFF: sign-in, org creation, membership, role enforcement (403 vs 404), last-owner 409, workspaces, UI pages, deep link through login, audit rows checked directly in Postgres, and no token anywhere in browser-facing responses or container logs.
- **Dry run against `main` before batch 3 merged: 4 pass / 8 fail, as expected.** Passing: alice signs in and is provisioned; the first API call provisions exactly one user and one `user.provisioned` audit row per user (3 and 3); an anonymous deep link redirects to login and returns to it; no token reached the browser. The 8 failures are exactly the tests that need the org and workspace routes, which return 404 until M01.02.02/04 merge. The secrets check and cleanup were clean.
- **Harness lessons:** user rows are created on the first authenticated API call, not at web login, so the test makes explicit `/me` calls for every user. `PG_EXEC` uses `docker exec <project>-postgres-1`, since compose paths with spaces break when shell-split.
- **Status:** the M01 gate check. It must pass in full after batch 3 merges.

---

## M01.02.03 + M01.02.05 — Org switcher and workspace navigation
- **Owner:** web-tenancy-agent · **Branch:** `agent/m01-web-tenancy` (935d535, includes the WIP 0ee47aa) · merged to `main` as 9212def · **Status:** DONE
- **Delivered:** a typed API client built from contract §4; a TanStack Query provider with org-scoped keys; `/home` (org list, create org with inline 409); an org switcher (keyboard accessible, `queryClient.clear()` runs before navigation); `/o/[orgSlug]` (workspace list, create form for owner/admin only) and `/o/[orgSlug]/w/[workspaceSlug]` (sidebar with `aria-current`); a calm "not found or no access" page that doesn't distinguish unknown from unauthorized; skip link, focus handling and reduced-motion support.
- **Coordinator verification (on `main`):** web lint clean; typecheck clean; unit tests **95 passed**; build ok. `tests/e2e/web_tenancy` → **6/6**. The older `tests/e2e/web_auth` suite, which the agent hadn't run, → **12/12** with `E2E_REQUIRE_REFRESH=1` (real refresh). Both leave nothing behind and the server logs are clean.
- **Acceptance:** "Scope visibly changes and query cache clears" — the test asserts the event order `clear` then `navigate`, and a DOM recorder shows no state between the switch and the new page contains the previous org's workspace names. The agent mutation-checked this: removing either the content-hiding step or the clear makes it fail. "Deep links load authorized workspace" — a fresh-session deep link goes to login with `returnTo`, lands on the workspace after a real Dex login, and renders it with `aria-current="page"`.
- **API assumptions sent to the API agent:** `Workspace.role` is null for an org owner/admin without an explicit workspace membership; list responses are `{items, next_cursor|null}`; a member creating a workspace gets 403; a duplicate slug gets 409 `slug_taken`.
- **Follow-ups:** the root layout has no `.dark` toggle, so dark mode works only once something sets the class (M07.03.03). One vitest sub-case with a mocked rejecting `apiFetch` behaved oddly and was replaced by direct classification tests; the network-error path is covered elsewhere (`api-client.test.ts`).

---

## M01.02.02 + M01.02.04 — Organization and workspace REST API
- **Owner:** tenancy-api-agent · **Branch:** `agent/m01-tenancy-api` (f6bd378, 00851b2) · merged to `main` as d029aaf · **Status:** DONE
- **Files:** `apps/api/app/api/deps.py`, `apps/api/app/modules/{organizations,workspaces}/{schemas,service,router}.py`, `apps/api/tests/tenancy/`, `tests/integration/m01_tenancy/`, plus 8 lines in `app/main.py`. No migrations.
- **Delivered:** all 16 `/v1` routes of contract §4, each running principal → tenant transaction → role load → `decide()`, with denials audited in their own transaction, the DB-error to HTTP mapping, keyset pagination, and an audit event per mutation.
- **Coordinator verification (on `main`, through the Linux runner):** API unit **856 passed**; `tests/integration/m01_tenancy` **45 passed** (including the real-Dex flow); strict mypy **no issues in 50 files**; ruff clean; the full-stack gate **12/12**.
- **Acceptance:** "CRUD enforces membership roles": the owner/admin/member/outsider matrix on every endpoint, distinguishing 404 from 403; admins cannot grant or revoke owner; last-owner 409 on demote and remove; soft delete returns 404 on all sub-routes; cross-org id guessing returns 404; an audit row per mutation and per denial. "Workspace CRUD and memberships tested": passed.
- **Coordinator changes after merge (DC-018):** slugs are strictly lower-case at the API (422 `invalid_request` for upper-case, never `slug_taken`), with the unit, integration and e2e tests updated. The `0002` migration was formatted.
- **Contract:** as implemented, in `m01-contracts.md` §7 (DC-019).

## Linux test runner (coordinator, DC-018)
- **Why:** Application Control blocks psycopg's compiled driver and mypyc everywhere on the host as of 2026-11-18.
- **What:** `scripts/dev/linux-runner/{Dockerfile,run.sh}`. It runs a command in a Linux container with the repo mounted at Docker Desktop's VM path (so compose bind mounts resolve), host networking, and a persistent venv volume.
- **Verified:** API unit 856, all five DB suites, 324 RLS, mypy, and 11 infrastructure tests all ran through it with results identical to the agent's own runs.

## M01 gate: full-stack e2e
`tests/e2e/fullstack_m01` → **12/12** on the merged `main` (see `PROJECT_STATUS.md`).
