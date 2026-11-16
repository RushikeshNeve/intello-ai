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
| (foundation) | DB layer, ORM models, security core, Alembic, api-migrate, test port overlay | foundation-agent | agent/m01-foundation | contracts | IN_PROGRESS |
| M01.02.01 | Organization schema and migration | foundation-agent (migration owner) | agent/m01-foundation | contracts | IN_PROGRESS |
| M01.01.02 | Web session handling (BFF, PKCE, HttpOnly) | web-session-agent | agent/m01-web-session | Dex, contracts | IN_PROGRESS |
| M01.03.01 | RBAC policy decision service | policy-agent | agent/m01-policy | contracts | DONE (632 on main; full API suite 640) |
| M01.01.01 | OIDC issuer/audience/expiry verification | — | — | foundation (security.py) | TODO |
| M01.01.03 | User provisioning on first sign-in | — | — | foundation (models) | TODO |
| M01.03.02 | PostgreSQL RLS regression tests | — | — | M01.02.01 | TODO |
| M01.03.03 | Document grant model contracts | — | — | foundation, M01.03.01 | TODO |
| M01.03.04 | Auditable security events | — | — | foundation | TODO |
| M01.02.02 | Organization REST endpoints | — | — | 01.01, 01.03, 03.01, 03.04 | TODO |
| M01.02.04 | Workspace schema and endpoints | — | — | as 02.02 | TODO |
| M01.02.03 | Organization switcher | — | — | 01.02, 02.02 | TODO |
| M01.02.05 | Workspace navigation | — | — | 02.03, 02.04 | TODO |

Parallel-safety: foundation owns `apps/api/**` except `authorization/policy.py`, plus `apps.yml` and `compose_support.py`. The policy agent owns only `policy.py` and its tests. The web agent owns `apps/web/**` auth paths and the web manifest, with isolated ports 23300/28380.
