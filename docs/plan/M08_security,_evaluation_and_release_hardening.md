# M08 — Security, evaluation and release hardening

**Status:** Not started. **Definition of done:** each leaf task passes acceptance checks, review, security checks, and required integration tests.

## M08.01 — Security

### M08.01.01 — Run tenant and resource-grant adversarial suite

- **Status:** TODO
- **Scope / owned path:** `tests/security/isolation/`
- **Prerequisite:** M07 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** No unauthorized retrieval or graph counts.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M08.01.02 — Run prompt injection and tool permission tests

- **Status:** TODO
- **Scope / owned path:** `tests/security/ai_injection/`
- **Prerequisite:** M07 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Malicious source cannot modify policy.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M08.01.03 — Test full deletion lineage and cache invalidation

- **Status:** TODO
- **Scope / owned path:** `tests/security/deletion/`
- **Prerequisite:** M07 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Search/graph/memory/cache inaccessible after delete.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M08.01.04 — Validate credential rotation and connector revoke

- **Status:** TODO
- **Scope / owned path:** `tests/security/secrets/`
- **Prerequisite:** M07 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Rotation/revocation does not leak tokens.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M08.02 — Reliability

### M08.02.01 — Implement database/object backup automation

- **Status:** TODO
- **Scope / owned path:** `infrastructure/backup/`
- **Prerequisite:** M07 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** All stores backed up with checksums.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M08.02.02 — Perform full restore rehearsal

- **Status:** TODO
- **Scope / owned path:** `tests/disaster_recovery/`
- **Prerequisite:** M07 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Catalog, files, graph and index checks pass.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M08.02.03 — Build load and fault-injection tests

- **Status:** TODO
- **Scope / owned path:** `tests/performance/`
- **Prerequisite:** M07 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** p95, worker recovery and quota behavior recorded.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M08.02.04 — Add structured tracing and local dashboards

- **Status:** TODO
- **Scope / owned path:** `infrastructure/observability/`
- **Prerequisite:** M07 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** API to worker to AI spans correlated.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M08.03 — Release

### M08.03.01 — Create CI quality and contract gates

- **Status:** TODO
- **Scope / owned path:** `.github/workflows/ci.yml`
- **Prerequisite:** M07 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Lint, tests, dependency checks and build gated.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M08.03.02 — Document installation and security runbooks

- **Status:** TODO
- **Scope / owned path:** `docs/operations/`
- **Prerequisite:** M07 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Reproducible clean-machine setup and incident steps.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M08.03.03 — Run acceptance suite and release checklist

- **Status:** TODO
- **Scope / owned path:** `docs/releases/mvp-checklist.md`
- **Prerequisite:** M07 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** All P0 gates and evidence attached.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M08.03.04 — Tag baseline and record deferrals

- **Status:** TODO
- **Scope / owned path:** `docs/releases/mvp-release.md`
- **Prerequisite:** M07 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Release version and unsupported features explicit.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.
