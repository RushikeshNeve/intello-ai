# Session Handoff — M01 complete, 2026-11-18 · next: M02

## State
- **M00: 13/13 DONE. M01: 12/12 DONE, exit gate PASSED** (`PROJECT_STATUS.md`). 25 of 108 tasks done.
- `main` is the source of truth; all agent branches are merged. No agents are running. Docker may be running; no UKOS project containers should be.
- What works end to end on `main`: sign-in through Dex → web BFF with HttpOnly sessions → API with OIDC verification, user provisioning, orgs, workspaces, roles, grants, audit, and row-level security. The UI has the org switcher, workspace navigation and deep links. Run it with `docker compose up -d --wait` (runbook §3).

## Next: M02 — Document library and object storage (11 tasks)
Declared prerequisite: M01 exit gate (met). Task list: `docs/plan/M02_document_library_and_object_storage.md` and `ACTIVE_TASKS`-style table to add. Suggested order, honouring the ownership rules in `DEPENDENCY_GRAPH.md`:
1. **Foundation (coordinator, single migration owner):** migration `0003` for `documents`, `document_versions`, `sources` with immutable-version constraints, RLS (reuse the M01 patterns: `ORG_SCOPED_TABLES`, FORCE RLS, fail-closed, no hard deletes), and the partial unique index on active `resource_grants` (DC-015 follow-up). This is M02.02.01.
2. **Parallel batch A (no shared files):** M02.01.01 storage port (`modules/storage/ports.py`); then M02.01.02 RustFS adapter (`infrastructure/object_storage/`, plain S3 only: path-style SigV4, `x-amz-checksum-sha256`, `If-None-Match: *`, separate presign client on `S3_PUBLIC_ENDPOINT_URL`; see `docs/spikes/object-storage.md`); M02.03.01 upload component (web).
3. **Batch B after the migration:** M02.01.03 upload sessions with quotas, M02.01.04 finalize with checksum verification, M02.02.02 catalog CRUD (grants enforced via `active_permissions` + `decide`), M02.02.03 version timeline, M02.02.04 deletion = logical denial first.
4. **Batch C (UI):** M02.03.02 library table (filters in the URL), M02.03.03 versioned preview.
Write the M02 contract (`docs/architecture/m02-contracts.md`) before parallelizing: upload-session API, finalize semantics, document and version shapes, error codes, audit events, deletion states.

## Reminders (this machine)
- **Run every test that imports psycopg, and mypy, through `scripts/dev/linux-runner/run.sh`** (runbook §6). Node/HTTP suites (`tests/e2e/*`) and `ruff` run on the host. Give each suite its own `UKOS_TEST_COMPOSE_PROJECT` and ports.
- Dex is pinned to v2.45.1. Quit Docker from the tray; if it crashes at start run `scripts/windows/fix-docker-sockets.ps1 -Start`.
- Weekend schedule: development happens on weekends (see memory).
