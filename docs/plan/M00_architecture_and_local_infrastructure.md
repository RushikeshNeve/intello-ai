# M00 — Architecture and local infrastructure

**Status:** Not started. **Definition of done:** each leaf task passes acceptance checks, review, security checks, and required integration tests.

## M00.01 — Architecture contracts

### M00.01.01 — Record system boundaries and ADR index

- **Status:** TODO
- **Scope / owned path:** `docs/architecture/system-boundaries.md`
- **Prerequisite:** None
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Boundary table and accepted/provisional ADR references are consistent.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M00.01.02 — Define local environment variables

- **Status:** TODO
- **Scope / owned path:** `infrastructure/env/.env.example`
- **Prerequisite:** None
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Every service variable documented with safe placeholder.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M00.01.03 — Create developer runbook

- **Status:** TODO
- **Scope / owned path:** `docs/development/local-runbook.md`
- **Prerequisite:** None
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Clean-machine setup, startup and teardown steps documented.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M00.02 — Local services

### M00.02.01 — Compose PostgreSQL and Redis

- **Status:** TODO
- **Scope / owned path:** `infrastructure/compose/data.yml`
- **Prerequisite:** None
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Containers boot with health checks and volumes.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M00.02.02 — Compose RushDB with persistence

- **Status:** TODO
- **Scope / owned path:** `infrastructure/compose/rushdb.yml`
- **Prerequisite:** None
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Restart preserves graph and vector records.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M00.02.03 — Evaluate local S3-compatible storage

- **Status:** TODO
- **Scope / owned path:** `docs/spikes/object-storage.md`
- **Prerequisite:** None
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** API tests cover upload, signed reads and deletion.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M00.02.04 — Compose local object-store adapter

- **Status:** TODO
- **Scope / owned path:** `infrastructure/compose/objects.yml`
- **Prerequisite:** None
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Object integration tests succeed.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M00.02.05 — Add FastAPI and Next.js starter services

- **Status:** TODO
- **Scope / owned path:** `infrastructure/compose/apps.yml`
- **Prerequisite:** None
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Web and API health endpoints respond.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M00.03 — Risk spikes

### M00.03.01 — Implement RushDB tenant isolation spike

- **Status:** TODO
- **Scope / owned path:** `tests/spikes/rushdb_tenants.py`
- **Prerequisite:** None
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Cross-tenant read and traversal tests deny leakage.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M00.03.02 — Implement RushDB hybrid retrieval spike

- **Status:** TODO
- **Scope / owned path:** `tests/spikes/rushdb_hybrid.py`
- **Prerequisite:** None
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** BM25, vector and filtered fusion return expected fixtures.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M00.03.03 — Implement graph provenance spike

- **Status:** TODO
- **Scope / owned path:** `tests/spikes/rushdb_graph.py`
- **Prerequisite:** None
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Evidence-linked traversal and deletion tests pass.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M00.03.04 — Benchmark local embeddings and generation

- **Status:** TODO
- **Scope / owned path:** `docs/spikes/local-models.md`
- **Prerequisite:** None
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Record memory, speed, accuracy and chosen fallback.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M00.03.05 — Verify backup and restore spike

- **Status:** TODO
- **Scope / owned path:** `scripts/restore_smoke.sh`
- **Prerequisite:** None
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Restored data matches fixture checksums.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.
