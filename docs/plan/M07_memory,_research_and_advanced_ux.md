# M07 — Memory, research and advanced UX

**Status:** Not started. **Definition of done:** each leaf task passes acceptance checks, review, security checks, and required integration tests.

## M07.01 — Memory

### M07.01.01 — Define approved memory model and lifecycle

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/memory/models.py`
- **Prerequisite:** M06 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Owner, scope, evidence, approval and expiry recorded.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M07.01.02 — Build memory suggestion and approval APIs

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/memory/api.py`
- **Prerequisite:** M06 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Unapproved suggestion never becomes durable fact.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M07.01.03 — Implement evidence-change memory invalidation

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/memory/invalidation.py`
- **Prerequisite:** M06 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Source revocation invalidates derived memories.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M07.01.04 — Build memory approval interface

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/memory/`
- **Prerequisite:** M06 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Approve/reject and provenance preview work.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M07.02 — Research workflows

### M07.02.01 — Implement saved research sessions

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/research/sessions.py`
- **Prerequisite:** M06 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Sessions recover after interruption.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M07.02.02 — Implement bounded research planner

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/research/planner.py`
- **Prerequisite:** M06 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Task count, cost and scope limits enforced.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M07.02.03 — Generate sourced research reports

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/research/reports.py`
- **Prerequisite:** M06 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Every sourced statement has traceable evidence.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M07.02.04 — Build research workspace and saved outputs

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/research/`
- **Prerequisite:** M06 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Users reopen reports and citations.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M07.03 — UX refinement

### M07.03.01 — Implement command palette and keyboard navigation

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/command-palette/`
- **Prerequisite:** M06 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Main workflows usable from keyboard.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M07.03.02 — Implement source-locator viewers for code and sheets

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/evidence/renderers/`
- **Prerequisite:** M06 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Exact line and cell ranges open.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M07.03.03 — Implement light/dark and responsive panels

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/workspace/layout/`
- **Prerequisite:** M06 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Responsive and reduced-motion tests pass.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.
