# M06 — Connectors and universal formats

**Status:** Not started. **Definition of done:** each leaf task passes acceptance checks, review, security checks, and required integration tests.

## M06.01 — Connector framework

### M06.01.01 — Define source connector port and sync cursor

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/connectors/contracts.py`
- **Prerequisite:** M05 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Cursor replay and authentication contract documented.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M06.01.02 — Build safe URL capture processor

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/connectors/url_capture.py`
- **Prerequisite:** M05 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** SSRF, redirect and size tests pass.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M06.01.03 — Build connector token encryption abstraction

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/connectors/secrets.py`
- **Prerequisite:** M05 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Secrets never appear in logs.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M06.01.04 — Implement scheduler and webhook ingestion

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/connectors/scheduler.py`
- **Prerequisite:** M05 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Duplicate notification safe.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M06.01.05 — Implement first connector discovery and delta sync

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/connectors/provider_one/`
- **Prerequisite:** M05 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** New/update/delete delta fixture succeeds.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M06.01.06 — Implement access revocation reconciliation

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/connectors/revocation.py`
- **Prerequisite:** M05 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Revoked source immediately blocked.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M06.02 — Format expansion

### M06.02.01 — Add CSV/XLSX structural extraction

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ingestion/processors/spreadsheet.py`
- **Prerequisite:** M05 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Sheet/cell citations verified.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M06.02.02 — Implement repository file and commit locators

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ingestion/processors/code.py`
- **Prerequisite:** M05 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** File path and commit pinned.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M06.02.03 — Add image OCR processor contract

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ingestion/processors/image.py`
- **Prerequisite:** M05 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** OCR provenance and graceful no-model fallback.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M06.02.04 — Add audio transcript processor contract

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ingestion/processors/audio.py`
- **Prerequisite:** M05 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Timestamped passages and size limits verified.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M06.02.05 — Add video processing capability flag

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ingestion/processors/video.py`
- **Prerequisite:** M05 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Unsupported hardware/config gives explicit status.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.
