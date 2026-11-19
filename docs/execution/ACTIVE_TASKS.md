# Active Tasks

Statuses: `TODO → READY → IN_PROGRESS → REVIEW → DONE`, plus `BLOCKED` / `FAILED`.
A task is DONE only after the coordinator reruns its acceptance check on `main` (evidence in `AGENT_HANDOFFS.md`).

## M00 — complete (2026-11-16) · exit gate PASSED (conditional), see PROJECT_STATUS.md

| Task | Title | Owner (agent) | Branch | Practical deps | Status |
|---|---|---|---|---|---|
| M00.01.01 | System boundaries + ADR index | coordinator | main | — | DONE |
| M00.01.02 | Local env contract | coordinator | main | — | DONE (contract test 7/7, mutation-checked) |
| M00.02.01 | Compose PostgreSQL + Redis | platform-agent | agent/m00-platform | M00.01.02 | DONE (f6d04a8 merged early; 5/5 on main) |
| M00.02.05 | FastAPI + Next.js starters + apps.yml | platform-agent | agent/m00-platform | M00.02.01 | DONE (all checks on main; typecheck fixed by coordinator) |
| M00.02.02 | Compose RushDB with persistence | rushdb-agent | agent/m00-rushdb | M00.01.02 | DONE (verified on main 52/52) |
| M00.03.01 | RushDB tenant isolation spike | rushdb-agent | agent/m00-rushdb | M00.02.02 | DONE (verified on main 52/52) |
| M00.03.02 | RushDB hybrid retrieval spike | rushdb-agent | agent/m00-rushdb | M00.02.02 | DONE (verified on main 52/52) |
| M00.03.03 | Graph provenance spike | rushdb-agent | agent/m00-rushdb | M00.02.02 | DONE (verified on main 52/52; CONDITIONAL GO, DC-009) |
| M00.02.03 | Evaluate local S3-compatible storage | objectstore-agent | agent/m00-objectstore | — | DONE (RustFS chosen; verified on main) |
| M00.02.04 | Compose local object store | objectstore-agent | agent/m00-objectstore | M00.02.03 | DONE (integration 5/5 on main) |
| M00.03.04 | Benchmark local embeddings + generation | models-agent | agent/m00-models | — | DONE (LM Studio; accuracy reproduced exactly on main) |
| M00.03.05 | Backup/restore smoke | coordinator | main | 02.01, 02.02, 02.04 | DONE (passed, exit 0) |
| M00.01.03 | Developer runbook | coordinator | main | 02.01–02.05 | DONE (full-stack start/verify/teardown executed) |

### Why these groupings are parallel-safe
- The four agents own disjoint paths: `data.yml`/`apps.yml`/`apps/**`, `rushdb.yml`/`tests/spikes/rushdb_*`, `objects.yml`/`docs/spikes/object-storage.md`/`tests/spikes/objectstore*`, `docs/spikes/local-models.md`/`tests/spikes/models*`.
- None of them edits `infrastructure/env/.env.example`, root `compose.yaml`, or `docs/execution/*`. They report needed env changes in their handoff, and the coordinator applies them.
- Each agent runs Docker under its own compose project name (`ukos-<agent>`) on its own host ports from the UKOS block, so containers and volumes never collide.
- Tasks that share a service (the three RushDB spikes) run sequentially inside one agent because they share a fixture helper.

## M01 — Batch 1 (started 2026-11-16) · contract: `docs/architecture/m01-contracts.md` (frozen)

| Task | Title | Owner (agent) | Branch | Practical deps | Status |
|---|---|---|---|---|---|
| (prep) | Local OIDC provider Dex + test users (DC-012) | coordinator | main | — | DONE (tokens verified) |
| (prep) | Schema-first M01 contracts | coordinator | main | — | DONE |
| (foundation) | DB layer, ORM models, security core, Alembic, api-migrate, test port overlay | foundation-agent | agent/m01-foundation | contracts | DONE (verified on main) |
| M01.02.01 | Organization schema and migration | foundation-agent (migration owner) | agent/m01-foundation | contracts | DONE (46 DB tests on main) |
| M01.01.02 | Web session handling (BFF, PKCE, HttpOnly) | web-session-agent | agent/m01-web-session | Dex, contracts | DONE (unit 67, e2e 12/12 incl. real refresh) |
| M01.03.01 | RBAC policy decision service | policy-agent | agent/m01-policy | contracts | DONE (632 on main; full API suite 640) |
| M01.01.01 | OIDC issuer/audience/expiry verification | identity-agent | agent/m01-identity | foundation | DONE (unit + real Dex 17) |
| M01.01.03 | User provisioning on first sign-in | identity-agent | agent/m01-identity | foundation | DONE (idempotent, 10-thread race) |
| M01.03.02 | PostgreSQL RLS regression tests | rls-agent | agent/m01-rls | M01.02.01 | DONE (324 on main; gap fixed, DC-016) |
| M01.03.03 | Document grant model contracts | grants-agent | agent/m01-grants | foundation, M01.03.01 | DONE (unit 693, DB 21+46 on main) |
| M01.03.04 | Auditable security events | audit-agent | agent/m01-audit | foundation | DONE (unit 45, DB 15) |
| M01.02.02 | Organization REST endpoints | tenancy-api-agent | agent/m01-tenancy-api | batch 2 | DONE (45 DB + real Dex on main; gate e2e 12/12) |
| M01.02.04 | Workspace schema and endpoints | tenancy-api-agent | agent/m01-tenancy-api | batch 2 | DONE (schema from 0001; endpoints + memberships tested) |
| M01.02.03 | Organization switcher | web-tenancy-agent | agent/m01-web-tenancy | 01.02 + contract §4 | DONE (unit 95, e2e 6/6 + web_auth 12/12 on main) |
| M01.02.05 | Workspace navigation | web-tenancy-agent | agent/m01-web-tenancy | 01.02 + contract §4 | DONE (deep link through login verified) |

Parallel-safety: foundation owns `apps/api/**` except `authorization/policy.py`, plus `apps.yml` and `compose_support.py`. The policy agent owns only `policy.py` and its tests. The web agent owns `apps/web/**` auth paths and the web manifest, with isolated ports 23300/28380.

## M02 — Document library and object storage (started 2026-11-18) · contract: `docs/architecture/m02-contracts.md` (frozen)

| Task | Title | Owner (agent) | Branch | Practical deps | Status |
|---|---|---|---|---|---|
| (prep) | M02 contract; per-checkout runner venv | coordinator | main | M01 gate | DONE |
| M02.02.01 | Documents, versions, sources migrations (+ models, settings, shared schemas, router skeleton) | foundation-agent (migration owner) | agent/m02-foundation | contract | DONE (134 DB tests + RLS 396 on main) |
| M02.01.01 | S3-compatible storage port | storage-agent | agent/m02-storage | contract | DONE (unit 90) |
| M02.01.02 | Local object-store adapter (RustFS) + in-memory fake | storage-agent | agent/m02-storage | M02.01.01 | DONE (47 against real RustFS) |
| M02.03.01 | Drag/drop upload component | web-upload-agent | agent/m02-web-upload | contract | DONE (web unit 190, lint/typecheck/build clean on main; 3 mutation checks) |
| M02.01.03 | Upload sessions with quotas | upload-api-agent | agent/m02-upload-api | batch A | DONE (102 + 3 title tests; real RustFS e2e) |
| M02.01.04 | Finalize uploads with checksum verification | upload-api-agent | agent/m02-upload-api | 02.01.03 | DONE (missing/altered/orphan rejected; concurrency tested) |
| M02.02.02 | Document catalog CRUD | catalog-api-agent | agent/m02-catalog-api | batch A | DONE (97 catalog tests, EXPLAIN-checked) |
| M02.02.03 | Version timeline and retention status | catalog-api-agent | agent/m02-catalog-api | batch A | DONE (no version-mutating route; trigger proofs) |
| M02.02.04 | Deletion request and access denial | catalog-api-agent | agent/m02-catalog-api | batch A | DONE (immediate 404 on every path; gate e2e) |
| M02.03.02 | Searchable document table | web-library-agent | agent/m02-web-library | 03.01, contract | DONE (web 274 + e2e 14/14; URL is the state) |
| M02.03.03 | Versioned document preview | web-library-agent | agent/m02-web-library | 03.02, contract | DONE (viewer tests; real-browser PDF/CORS check pending) |
| (coverage) | Adversarial RLS tests for the 4 new tables | — | — | 02.02.01 | TODO (carried: first test-only task of the M03 batch; also M08.01.01) |
| (gate) | M02 full-stack e2e + exit gate | coordinator | main | all | DONE (13/13 on real stack with RustFS) |

Parallel-safety (batch A): the foundation agent owns migrations, `models.py`, `config.py`, the `documents/` package skeleton, `apps.yml` and `.env.example`; the storage agent owns `modules/storage`, `infrastructure/object_storage` and `pyproject.toml`/`uv.lock` (its adapter reads its own settings class, so it needs nothing from the foundation); the web agent owns `apps/web/src/features/documents/upload` and the web manifest. Each uses its own Docker project and ports.
