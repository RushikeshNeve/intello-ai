# M01 — Identity, organizations and workspaces

**Status:** Not started. **Definition of done:** each leaf task passes acceptance checks, review, security checks, and required integration tests.

## M01.01 — Identity

### M01.01.01 — Integrate local OIDC issuer verification

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/identity/oidc.py`
- **Prerequisite:** M00 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Invalid issuer, audience and expired tokens rejected.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M01.01.02 — Build session handling in web

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/auth/`
- **Prerequisite:** M00 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Login/logout and HttpOnly cookie flow tested.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M01.01.03 — Create user provisioning on first sign-in

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/identity/provisioning.py`
- **Prerequisite:** M00 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Idempotent user creation confirmed.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M01.02 — Tenancy

### M01.02.01 — Create organization schema and migration

- **Status:** TODO
- **Scope / owned path:** `apps/api/migrations/versions/`
- **Prerequisite:** M00 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Unique organizations and owner membership constraints pass.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M01.02.02 — Implement organization REST endpoints

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/organizations/api.py`
- **Prerequisite:** M00 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** CRUD enforces membership roles.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M01.02.03 — Build organization switcher

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/organizations/`
- **Prerequisite:** M00 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Scope visibly changes and query cache clears.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M01.02.04 — Create workspace schema and endpoints

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/workspaces/`
- **Prerequisite:** M00 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Workspace CRUD and memberships tested.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M01.02.05 — Build workspace navigation

- **Status:** TODO
- **Scope / owned path:** `apps/web/src/features/workspaces/`
- **Prerequisite:** M00 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Deep links load authorized workspace.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

## M01.03 — Permissions

### M01.03.01 — Implement RBAC policy decision service

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/authorization/policy.py`
- **Prerequisite:** M00 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Role matrix tests cover allow/deny cases.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M01.03.02 — Implement PostgreSQL RLS and regression tests

- **Status:** TODO
- **Scope / owned path:** `tests/security/postgres_rls/`
- **Prerequisite:** M00 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Cross-organization SQL access denied.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M01.03.03 — Add document grant model contracts

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/authorization/grants.py`
- **Prerequisite:** M00 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Grant, revoke and expiration semantics validated.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.

### M01.03.04 — Record auditable security events

- **Status:** TODO
- **Scope / owned path:** `apps/api/app/modules/audit/`
- **Prerequisite:** M00 exit gate
- **Agent deliverable:** one focused implementation or documentation change plus tests and PR description.
- **Acceptance:** Membership and role changes create redacted audit records.
- **Checks:** relevant unit/integration/security tests; lint/typecheck where applicable; include evidence in PR.
- **Handoff:** list changed files, commands run, test results, assumptions and follow-up work.
