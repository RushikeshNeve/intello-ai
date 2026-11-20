# Project Status

**Updated:** 2026-11-19 · **Current milestone:** M06 complete (smoke gate PASSED) → M07 (memory/research/UX) in progress
**Source of truth:** this Git repository. Notion mirror: **not synchronized** (no Notion integration in this session).

## Summary

| Milestone | Tasks | DONE | In progress | Gate |
|---|---|---|---|---|
| M00 Architecture & local infra | 13 | **13** | 0 | **PASSED (conditional)**, 2026-11-16 |
| M01 Identity, orgs, workspaces | 12 | **12** | 0 | **PASSED**, 2026-11-18 |
| M02 Documents & object storage | 11 | **11** | 0 | **PASSED**, 2026-11-19 |
| M03 Reliable ingestion | 14 | **14** | 0 | **PASSED**, 2026-11-19 (17/17 full-stack scenarios) |
| M04 Search & grounded chat | 13 | **13** | 0 | **PASSED** (lean smoke on the live stack with real LM Studio), 2026-11-19 |
| M05 Graph & GraphRAG | 11 | **11** | 0 | **PASSED** (lean smoke on the live stack with real LM Studio), 2026-11-19 |
| M06 Connectors & formats | 11 | **11** | 0 | **PASSED** (lean smoke on the live stack), 2026-11-20 |
| M07 Memory, research, UX | 11 | 0 | 0 | blocked on M06 gate |
| M08 Security, eval, release | 12 | 0 | 0 | blocked on M07 gate |
| **Total** | **108** | **85** | **0** | |

## M00 exit gate (definition in `DEPENDENCY_GRAPH.md`, DC-007)

| Gate condition | Result | Evidence |
|---|---|---|
| All 13 M00 tasks DONE, with acceptance rerun by the coordinator on `main` | ✅ | `AGENT_HANDOFFS.md` (every task has a coordinator-verification line) |
| RushDB PoC verdict (M00.03.01–03) | ✅ **CONDITIONAL GO** | 52/52 on `main`; the conditions in DC-009 bind M03.03.01+ |
| Object-store choice accepted (M00.02.03) | ✅ RustFS 1.0.5, fallback SeaweedFS 4.53 | DC-002 closed; contract 21/21 and integration 5/5 on `main` |
| Backup/restore across all stores | ✅ | `scripts/restore_smoke.sh` PASSED, exit 0 |
| Local inference runtime and default models | ✅ LM Studio (user decision), embedding-gemma-300m (768 dimensions) + gemma-3-4b | DC-011; accuracy reproduced exactly |

**Conditions carried forward** (these are not blockers; they are binding requirements on later tasks):
1. RushDB: only the API holds the token; the server derives the tenant from a Postgres-validated org; one adapter injects the workspace and revision filters; never use `query.text`; version pinned; spikes rerun on every upgrade (DC-009).
2. LM Studio is a **local-development runtime only** (licence forbids SaaS). Hosted deployments use a permissive `/v1` server (DC-011).
3. Windows Application Control on the dev machine: tools run as `python -m`; venvs are built from python.org Python; mypy runs in a container (DC-010, runbook §7.1).

## M01 exit gate (2026-11-18)

| Gate condition | Result | Evidence |
|---|---|---|
| All 12 M01 tasks DONE, acceptance rerun by the coordinator on `main` | ✅ | `AGENT_HANDOFFS.md` |
| Full-stack e2e on the real merged stack (`tests/e2e/fullstack_m01`) | ✅ **12/12** | web + API (with migrations) + Dex + Postgres, alice/bob/mallory: roles (403 vs 404), last-owner 409, workspaces, UI pages, deep link through login, audit rows in Postgres, no token in browser responses or container logs |
| Tenant isolation | ✅ | 324 adversarial RLS tests; org/workspace API tests prove other orgs are 404 |
| Static checks | ✅ | ruff clean, strict mypy on 50 files, web lint/typecheck |

Final suite on `main` (each isolated; nothing left behind): API unit 856; DB 46 foundation + 21 grants + 15 audit + 17 identity (real Dex) + 45 tenancy (real Dex); RLS 324; infrastructure 11; web unit 95; web e2e 12 (auth, real refresh) + 6 (tenancy); full-stack gate 12.

**Conditions carried forward:**
1. All database tests and mypy run through `scripts/dev/linux-runner/run.sh` on this machine (DC-018).
2. Web sessions are in-memory (one web process); a shared store is needed before scaling out (DC-017).
3. M08: org-membership checks in RLS policies, the `user_not_found` existence probe (replace with invitations), a database-level last-owner guarantee, repo-level ruff config for `tests/`.

## M02 exit gate (2026-11-19)

| Gate condition | Result | Evidence |
|---|---|---|
| All 11 M02 tasks DONE, acceptance rerun by the coordinator on `main` | ✅ | `AGENT_HANDOFFS.md` |
| Full-stack e2e on the real stack incl. RustFS (`tests/e2e/fullstack_m02`) | ✅ **13/13** | browser-style direct upload with the signed headers, bucket CORS (web origin allowed, unknown origin not), finalize + idempotent repeat, exact-byte download, html forced to attachment, version 2 never overwrites or renames, a tampered upload rejected by the store then a correct retry, oversize 413, viewer 403 / outsider 404, the org quota counting pending sessions, library and viewer pages, deletion denying access immediately for everyone, audit rows without file names, and no token, secret or presigned URL in the browser responses or container logs |
| Immutability and isolation | ✅ | 145 DB tests (a version can't be edited after creation), 396 row-level-security attack tests |
| Regression | ✅ | API unit 1,062; M01 DB suites 46/21/15/17/45; storage 47 on real RustFS; upload 105; catalog 97; web unit 274; web e2e 14 + 6 + 12; strict mypy 83 files; ruff clean |

**Conditions carried forward:**
1. **Not yet verified in a real browser:** PDF in a sandboxed iframe, inline image, and the plain-text preview's cross-origin fetch (needs the bucket CORS GET rule). Everything else is covered over HTTP. Check during manual testing; download is the fallback.
2. **Adversarial RLS tests for the four new tables** (the foundation agent added per-table isolation and smoke tests; the deep suite like M01's is still to write). First test-only task of the next batch, and part of M08.01.01.
3. Physical purge of deleted documents (the object delete, `purged_at`) is a later job (M03/M08); presigned URLs issued before a deletion stay valid until they expire (at most 5 minutes).
4. The type filter can express one exact type or one prefix only ("Unknown type" = application/octet-stream, "Excel (.xlsx)" only); a proper multi-type filter needs an API change.
5. Older maintained test folders still carry 56 style findings (M08.03.01).

## What exists now
- One-command local stack (`compose.yaml`): PostgreSQL 17, Redis 7.4, RushDB 3.0.10 (multitenant), RustFS 1.0.5 with bucket bootstrap, FastAPI API and Next.js 16 web, all loopback-only on the UKOS port block. Local AI is served by LM Studio on the host.
- Test suites: docs (4), env contract (7), data services (5), apps health (6), RushDB spikes (52), object-store contract (21) and integration (5), restore smoke, API unit tests (8) plus strict mypy, web lint, typecheck, unit test and build, local-model benchmark.
- Runbook: `docs/development/local-runbook.md`.

## Hardware and toolchain (audit 2026-11-16)
Docker 29.3.1 / Compose v5.1.1, Python 3.12.10 (python.org), uv 0.11.25, Node 24.14.1, npm 11.11.0, git 2.53, LM Studio 0.4.9.
i7-14650HX, 23.7 GB RAM (the Docker VM holds about 9.4 GB), RTX 5050 Laptop 8 GB.

## Open risks
1. RushDB 3 is very young (3.0.0 released 2026-10-29, about 11 releases since), and its deletes are not transactional.
2. RAM is tight when Docker and LM Studio run together on a 24 GB host. Don't load chat models over about 4 GB alongside the stack.
3. Gemma model licence terms are unreviewed (M08). The runner-up embedding model, qwen3-embedding-0.6b, is Apache-2.0.
4. The npm scaffold has audit advisories (to triage in M08.03.01).

See `ACTIVE_TASKS.md`, `BLOCKERS.md`, `DECISION_CHANGES.md`, `SESSION_HANDOFF.md`.

## M03 exit gate (2026-11-19)

| Gate condition | Result | Evidence |
|---|---|---|
| Full stack incl. real `worker` + `dispatcher` containers, RushDB, RustFS, stub embeddings | ✅ 17/17 | `bash tests/e2e/fullstack_m03/run.sh` (run by the agent twice, then by the coordinator on `main`, exit 0, log scan clean) |
| Upload → ready for TXT/MD/HTML/PDF; unsupported / no-text / corrupt terminal states | ✅ | scenarios 1, 5a |
| Partial index never visible; new version isolation; idempotent re-delivery | ✅ | scenarios 3, 4, 6c |
| Crash injection (worker, dispatcher, RushDB, embedder), dead-letter + retry, reconcile | ✅ | scenarios 5b, 6a-e |
| Deletion: immediate denial, purge of chunks and artifacts | ✅ (vector index stats do not drop: DC-024(7), M08 item) | scenario 7 |
| Tenancy/security: concealed 404, no ports, role checks, no secrets in logs | ✅ | scenarios 8, 99; RLS suite 1552 |

Counts on `main`: API unit 1552; DB suites m03_foundation 250, m03_runtime 25, m03_pipeline 44, m03_status 52; RLS 1552; web unit 325; web e2e documents 21 / tenancy 6 / auth 12; strict mypy clean.

**Known items carried:** `unsupported` files still show Retry (decide in M06); no real-browser pass of the new UI yet (user manual test §5c); real LM Studio throughput unmeasured; first-upload ON CONFLICT bug (M02) fixed by the gate (dev stack needs rebuild); GraphRAG/Claim cleanup in RushDB deletion belongs to M05; physical purge of originals (object delete, `purged_at`) still open; in-memory web session store; style findings in older tests (M08.03.01).

## M04 gate (lean, 2026-11-19)
Smoke `node tests/e2e/smoke_dev/smoke.mjs` on the dev stack: upload → ready → search (chunk_id on snippet) → grounded chat answer with citation → evidence chunk → outsider 404 → off-topic abstains. Found + fixed: API container lacked RUSHDB_*/AI_BASE_URL (search 503). Counts on main: API unit 1998, web unit 464, RLS 1749, m04_chat 58, m04_retrieval 73, m04_persistence 64, eval 36 (+ web_search e2e 25). Deferred items: `TEST_DEBT.md`.

## M05 gate (lean, 2026-11-19)
`GRAPH=1 node tests/e2e/smoke_dev/smoke.mjs` on the dev stack (GRAPH_ENABLED=true): relationship document → async extraction (gemma-3-4b) → entities → neighborhood (4 nodes/3 edges) → claim with evidence → outsider 404 → graph-aided grounded chat answer. Found + fixed: extraction prompt/parser chunk-ref mismatch with the real model (all items dropped). Deferred: `TEST_DEBT.md` (entity search loads entities in Python; no web e2e for the explorer; no real-browser canvas check).

## M06 gate (lean, 2026-11-20)
`GRAPH=1 M06=1 node tests/e2e/smoke_dev/smoke.mjs` on the dev stack: CSV upload → ready → searchable; URL capture blocks 169.254.169.254 (422) and captures https://example.com → ready; GitHub source `octocat/Hello-World` sync succeeded (1 active item); outsider 404; revoke → state revoked, token wiped. Not exercised live: webhook (needs `CONNECTOR_ENCRYPTION_KEY`), token connectors, OCR/audio engines (disabled-by-default contracts only), repo locators on a code file through the pipeline. See `TEST_DEBT.md`. Note: first LM Studio call after idle can time out (cold model load); rerun succeeds.
