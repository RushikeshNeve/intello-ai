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
