# Project Status

**Updated:** 2026-11-18 · **Current milestone:** M01 complete, gate PASSED → M02 (Document Library & Object Storage) next
**Source of truth:** this Git repository. Notion mirror: **not synchronized** (no Notion integration in this session).

## Summary

| Milestone | Tasks | DONE | In progress | Gate |
|---|---|---|---|---|
| M00 Architecture & local infra | 13 | **13** | 0 | **PASSED (conditional)**, 2026-11-16 |
| M01 Identity, orgs, workspaces | 12 | **12** | 0 | **PASSED**, 2026-11-18 |
| M02 Documents & object storage | 11 | 1 | 3 | batch A: upload UI done; DB foundation and storage running |
| M03 Reliable ingestion | 14 | 0 | 0 | blocked on M02 gate |
| M04 Search & grounded chat | 13 | 0 | 0 | blocked on M03 gate |
| M05 Graph & GraphRAG | 11 | 0 | 0 | blocked on M04 gate |
| M06 Connectors & formats | 11 | 0 | 0 | blocked on M05 gate |
| M07 Memory, research, UX | 11 | 0 | 0 | blocked on M06 gate |
| M08 Security, eval, release | 12 | 0 | 0 | blocked on M07 gate |
| **Total** | **108** | **26** | **3** | |

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
