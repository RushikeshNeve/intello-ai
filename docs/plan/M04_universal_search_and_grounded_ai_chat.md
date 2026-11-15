# M04 — Universal search and grounded AI chat

**Status:** Not started. **Definition of done:** each leaf task passes acceptance checks, review, security checks, and required integration tests.

## M04.01 — Retrieval

### M04.01.01 — Define authorized retrieval scope

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/retrieval/scope.py`
- **Prerequisite:** M03 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Client cannot mint privileged scope.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M04.01.02 — Implement RushDB filtered keyword and vector search

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/retrieval/hybrid.py`
- **Prerequisite:** M03 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Cross-workspace fixtures filtered before answer.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M04.01.03 — Add fusion, reranking and evidence deduplication

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/retrieval/ranking.py`
- **Prerequisite:** M03 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Offline retrieval golden set tracked.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M04.01.04 — Implement provenance-aware citation resolver

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/retrieval/citations.py`
- **Prerequisite:** M03 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Every citation resolves to authorized version.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M04.02 — Generation

### M04.02.01 — Define model gateway and local provider adapter

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ai/gateway.py`
- **Prerequisite:** M03 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Model policy denies forbidden providers.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M04.02.02 — Implement evidence-first prompt assembly

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ai/context.py`
- **Prerequisite:** M03 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Only approved chunks enter context.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M04.02.03 — Implement answer verification and abstention

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ai/verification.py`
- **Prerequisite:** M03 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Unsupported claims are flagged or withheld.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M04.02.04 — Implement persistent chat runs and SSE stream

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/ai/chat.py`
- **Prerequisite:** M03 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Reconnect never duplicates run or tokens.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M04.03 — Experience and evaluation

### M04.03.01 — Build unified search UI

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/search/`
- **Prerequisite:** M03 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Scope, filters and links persist.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M04.03.02 — Build chat and citation navigation

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/chat/`
- **Prerequisite:** M03 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Source click opens correct evidence.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M04.03.03 — Build split-view PDF/text evidence viewer

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/evidence/`
- **Prerequisite:** M03 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Page and excerpt highlights resolve.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M04.03.04 — Create golden RAG evaluation dataset

- **Status:** TODO
- **Scope / owned path:** `tests/evaluation/datasets/`
- **Prerequisite:** M03 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Answerable, unanswerable and permission cases included.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M04.03.05 — Add CI retrieval and citation quality gates

- **Status:** TODO
- **Scope / owned path:** `tests/evaluation/`
- **Prerequisite:** M03 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Benchmark regression gate produces machine-readable report.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.
