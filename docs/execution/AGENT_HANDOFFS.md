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
