# M03 — Reliable knowledge ingestion

**Status:** Not started. **Definition of done:** each leaf task passes acceptance checks, review, security checks, and required integration tests.

## M03.01 — Task infrastructure

### M03.01.01 — Implement jobs and outbox schema

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ingestion/models.py`
- **Prerequisite:** M02 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Atomic create job + outbox transaction test passes.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M03.01.02 — Build outbox dispatcher

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/workers/dispatcher.py`
- **Prerequisite:** M03.01.01
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Crash/replay does not lose work.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M03.01.03 — Configure Redis Celery worker queues

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/workers/celery_app.py`
- **Prerequisite:** M03.01.02
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Routing and worker recovery verified.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M03.01.04 — Build job claims, leases and heartbeats

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ingestion/execution.py`
- **Prerequisite:** M03.01.03
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Concurrent workers cannot publish same job.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M03.02 — Processing

### M03.02.01 — Define canonical extracted-content contract

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ingestion/contracts.py`
- **Prerequisite:** M02 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Contains modality and source locators.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M03.02.02 — Implement PDF/TXT/MD/HTML processors

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ingestion/processors/`
- **Prerequisite:** M03.02.01
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Golden extraction fixtures pass.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M03.02.03 — Add normalization and chunking stages

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ingestion/chunking.py`
- **Prerequisite:** M03.02.02
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Stable chunks and source offsets produced.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M03.02.04 — Add checkpoint and artifact fingerprints

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ingestion/checkpoints.py`
- **Prerequisite:** M03.02.03
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Repeated stages reuse artifacts safely.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M03.02.05 — Implement local embedding adapter

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ai/embeddings/`
- **Prerequisite:** M03.02.04
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Dimension and model space validation passes.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M03.03 — Publication

### M03.03.01 — Create RushDB knowledge repository adapter

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/infrastructure/rushdb/repository.py`
- **Prerequisite:** M02 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Tenant-scoped CRUD and search contracts pass.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M03.03.02 — Implement versioned chunk indexing

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/knowledge/indexing.py`
- **Prerequisite:** M03.03.01
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Duplicate task does not duplicate knowledge.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M03.03.03 — Implement publication barrier

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/knowledge/publication.py`
- **Prerequisite:** M03.03.02
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Unfinished revisions never retrievable.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M03.03.04 — Build ingestion status SSE and UI

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/ingestion/`
- **Prerequisite:** M03.03.03
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Reconnection returns current persisted state.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M03.03.05 — Build retry, dead-letter and reconciliation tests

- **Status:** TODO
- **Scope / owned path:** `tests/integration/ingestion_recovery/`
- **Prerequisite:** M03.03.04
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Crash-injection suite recovers deterministically.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.
