# M02 — Document library and object storage

**Status:** Not started. **Definition of done:** each leaf task passes acceptance checks, review, security checks, and required integration tests.

## M02.01 — Object layer

### M02.01.01 — Implement S3-compatible storage port

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/storage/ports.py`
- **Prerequisite:** M01 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Port covers put/get/delete/sign and multipart contract.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M02.01.02 — Implement local object-store adapter

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/infrastructure/object_storage/`
- **Prerequisite:** M01 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Contract tests pass against local store.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M02.01.03 — Issue upload sessions with quotas

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/documents/upload.py`
- **Prerequisite:** M01 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Unauthorized/oversize uploads rejected.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M02.01.04 — Finalize uploads with checksum verification

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/documents/finalize.py`
- **Prerequisite:** M01 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Missing, altered or orphan objects rejected.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M02.02 — Document model

### M02.02.01 — Create documents, versions and sources migrations

- **Status:** TODO
- **Scope / owned path:** `apps/api/migrations/versions/`
- **Prerequisite:** M01 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Immutable version and foreign key invariants tested.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M02.02.02 — Build document catalog CRUD endpoints

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/documents/api.py`
- **Prerequisite:** M01 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Filtering/pagination and grants enforced.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M02.02.03 — Implement version timeline and retention status

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/documents/versions.py`
- **Prerequisite:** M01 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Versions cannot be silently overwritten.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M02.02.04 — Implement deletion request and access denial

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/documents/deletion.py`
- **Prerequisite:** M01 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Deleted items denied before physical purge.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M02.03 — Library UI

### M02.03.01 — Build drag/drop upload component

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/documents/upload/`
- **Prerequisite:** M01 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Upload retries and error states testable.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M02.03.02 — Build searchable document table

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/documents/library/`
- **Prerequisite:** M01 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Pagination and filters persist in URL.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M02.03.03 — Build basic versioned document preview

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/documents/viewer/`
- **Prerequisite:** M01 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Authorized version opens and unavailable state handled.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.
