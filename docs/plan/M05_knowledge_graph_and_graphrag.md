# M05 — Knowledge graph and GraphRAG

**Status:** Not started. **Definition of done:** each leaf task passes acceptance checks, review, security checks, and required integration tests.

## M05.01 — Graph modeling

### M05.01.01 — Define graph ontology and schema contracts

- **Status:** TODO
- **Scope / owned path:** `docs/architecture/knowledge-ontology.md`
- **Prerequisite:** M04 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Node and edge types and provenance specified.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M05.01.02 — Implement entity extraction stage

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/knowledge/entities.py`
- **Prerequisite:** M04 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Extractor records source and version IDs.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M05.01.03 — Implement entity resolution and canonical IDs

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/knowledge/resolution.py`
- **Prerequisite:** M04 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Conflicting identities remain reviewable.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M05.01.04 — Implement claim and evidence-backed edge writes

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/knowledge/claims.py`
- **Prerequisite:** M04 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Unsupported edges cannot appear verified.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M05.02 — Graph retrieval

### M05.02.01 — Implement permission-safe graph traversal

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/retrieval/graph.py`
- **Prerequisite:** M04 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Restricted-edge leakage tests pass.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M05.02.02 — Implement selective retrieval router

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/retrieval/router.py`
- **Prerequisite:** M04 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Basic questions do not require graph path.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M05.02.03 — Implement graph-enhanced evidence merge

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/retrieval/graph_merge.py`
- **Prerequisite:** M04 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Graph paths cite accessible original evidence.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M05.03 — Graph experience

### M05.03.01 — Expose bounded neighborhood graph API

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/knowledge/graph_api.py`
- **Prerequisite:** M04 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Pagination and node limits enforced.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M05.03.02 — Build contextual React Flow explorer

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/knowledge-graph/`
- **Prerequisite:** M04 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Expandable nodes with keyboard use.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M05.03.03 — Build edge provenance inspection

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/knowledge-graph/evidence/`
- **Prerequisite:** M04 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Selecting edge opens supporting citation.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M05.03.04 — Benchmark hybrid versus GraphRAG

- **Status:** TODO
- **Scope / owned path:** `tests/evaluation/graph/`
- **Prerequisite:** M04 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Quality and latency delta reported.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.
