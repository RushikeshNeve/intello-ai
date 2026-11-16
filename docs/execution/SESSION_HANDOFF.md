# Session Handoff — 2026-11-16, M00 complete

## State
- **M00: 13/13 DONE, exit gate PASSED (conditional).** Details in `PROJECT_STATUS.md`. Every task was verified by the coordinator on `main`.
- All agent branches (`agent/m00-platform`, `agent/m00-rushdb`, `agent/m00-objectstore`, `agent/m00-models`) are merged into `main`. Their worktrees under `.claude/worktrees/` are clean, and their work is in `main`; they can be removed.
- Docker: no UKOS containers are running. Volumes from the verification runs were removed. The Ollama volume was deleted at the user's request.
- LM Studio: server on 127.0.0.1:1234, `google/gemma-3-4b` loaded. The `google/embedding-gemma-300m` and `qwen3-embedding-0.6b` models were downloaded by the benchmark.

## Next: M01 (Identity, Organizations & Workspaces), 12 tasks
Declared prerequisite: M00 exit gate (met). Suggested first batch, honouring the ownership rules in `DEPENDENCY_GRAPH.md`:
1. **Decision first:** choose the local OIDC provider for M01.01.01 (ADR-026; candidates such as Keycloak, ZITADEL or Authentik). This picks a new compose service on port 8380, so record it in `DECISION_CHANGES.md`.
2. **Migration owner (coordinator):** set up Alembic in `apps/api/migrations/`, then M01.02.01 (organizations + memberships). Migrations stay serialized.
3. **Can run in parallel once (1) and (2) are fixed:** M01.03.01 RBAC policy service (pure logic), M01.01.01 OIDC verification, M01.03.04 audit events. Frontend tasks (M01.01.02, M01.02.03, M01.02.05) start after the org/workspace API contracts are written schema-first.
4. M01.03.02 RLS regression tests depend on the M01.02.01/M01.02.04 schemas.

## Reminders
- Run tests as `python -m pytest`. Build API venvs from python.org Python. Run mypy in a container.
- Quit Docker Desktop from the tray. Never kill it.
- Deny firewall prompts; everything binds to 127.0.0.1.
