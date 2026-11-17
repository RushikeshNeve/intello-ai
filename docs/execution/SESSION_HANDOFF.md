# Session Handoff — office closed 2026-11-17 (Tue) · next session: Saturday 2026-11-21

Development runs on weekends from now on. Nothing is running: no UKOS containers, no dev servers, no agents.

## Where things stand
- **M00: 13/13 DONE.** The exit gate passed with conditions (`PROJECT_STATUS.md`).
- **M01: 8/12 DONE**, all verified on `main` (commit `43873a1`, then tracking commits):
  - M01.01.01 OIDC verification
  - M01.01.02 web session (BFF, PKCE, HttpOnly)
  - M01.01.03 provisioning
  - M01.02.01 org schema and migration
  - M01.03.01 RBAC policy
  - M01.03.02 RLS suite
  - M01.03.03 grants
  - M01.03.04 audit
- **M01 batch 3 (4 tasks) was interrupted by the usage limit:**
  - **M01.02.02 + M01.02.04, org and workspace REST API:** the agent had made no changes, so its worktree was cleaned up. **Restart fresh** from its brief (below).
  - **M01.02.03 + M01.02.05, org switcher and workspace nav:** early partial work is saved as an **unreviewed WIP commit `0ee47aa`** on `agent/m01-web-tenancy`, in worktree `.claude/worktrees/agent-a54f81773383b317d`. It contains only the added shadcn components (badge, dropdown-menu, input, label) and package.json changes. **Resume or restart.** Don't merge without verification.

## Saturday plan
1. Start Docker Desktop and confirm it's up (`docker info`). Start the LM Studio server only if needed (`lms server start`); M01 doesn't use it.
2. Restart batch 3 in parallel. The briefs are the M01.02.02/04 and M01.02.03/05 assignments in `ACTIVE_TASKS.md` plus contract §4:
   - **Backend:** all 16 `/v1` routes in contract §4, using `decide()` / `is_concealed()` / `can_assign_org_role()`, audit `record_event` / `record_denied`, and the DB error mapping (409 `last_owner` / `slug_taken`). Test the role matrix against real Postgres, plus one real-Dex flow. Ports 25551/26551/28391.
   - **Frontend:** QueryClient with `clear()` on org switch, `/home`, `/o/[orgSlug]`, `/o/[orgSlug]/w/[workspaceSlug]`, an accessible switcher and nav, a deep link working through the login redirect, an e2e against a stub API. Ports 23310/23311/28392.
3. After both merge, the coordinator runs a **full-stack e2e**: the real web app, API and Dex together, alice/bob/mallory flows. Then evaluate the **M01 exit gate**.
4. Then M02: document library and object storage.

## Reminders (this machine)
- Run tools as `python -m …`. Put the API venv under `%LOCALAPPDATA%` (`UV_PROJECT_ENVIRONMENT`). Run mypy in a container (runbook §7.1).
- Dex is pinned to **v2.45.1**; v2.46.x hangs on refresh (DC-016).
- Quit Docker Desktop from the tray, never kill it. Deny firewall prompts.
- Every test suite must run on its own compose project and ports (see the commands in `AGENT_HANDOFFS.md`).
