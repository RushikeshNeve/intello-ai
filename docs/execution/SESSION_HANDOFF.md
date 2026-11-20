# Session Handoff — M07 done; M08 + M09 in progress (2026-11-20, usage limit reached)

## State
- **96/108 tasks done; gates passed M00–M07** (`PROJECT_STATUS.md`); `main` pushed to GitHub at M07 close (fc3f678). Local-only since then: M08 contract, M08 quality merge, M09 F0 audit + F1 shell merge, design-reference, backlog/plan docs (`main` ≈ dc11e71). Push at M08 gate (policy: after each milestone).
- Modes (memory): lean (`token-budget`), sanity tests only, `TEST_DEBT.md`; UI revamp = M09 (`ui-revamp-m09`); release policy: NO release before M09; M10 = free-VM production deployment plan in `REMAINING_PLAN.md`; post-M09 backlog `POST_M09_BACKLOG.md`.

## BLOCKER first: Docker Desktop engine is hung
Disk on C: hit 0 GB (Docker WSL data 55 GB on C:; I cleared the npm cache → ~7 GB free). The user must restart Docker Desktop (tray → Quit → start; if socket error: `scripts/windows/fix-docker-sockets.ps1 -Start`) and ideally move the disk image to D: (Settings → Resources → Advanced). Then: `docker builder prune` (~13 GB), `docker container prune` (only when no agent is mid-test), remove merged agent worktrees, rebuild dev stack (`docker compose up -d --build --wait`; `.env` has GRAPH_ENABLED=true and worker vars).

## In flight
- **M08 security agent** (a82c398f76cdd54db, branch agent/m08-security) and **M08 operations agent** (ac27db2e08501b8d8, branch agent/m08-operations): launched before the Docker failure, likely blocked on Docker; check their worktrees/branches, resume with SendMessage after Docker is back, or merge what exists.
- **M08 quality** merged (CI workflows, runbooks, OpenAPI snapshot, acceptance harness `tests/e2e/acceptance/`, `docs/release/{CHECKLIST,BASELINE}.md`): host-verified only. NOT yet run: strict mypy, full unit suite, `run_local.sh api audit`, **`bash tests/e2e/acceptance/run.sh`** (expect small fixes), the four web HTTP e2e suites for the F1 shell (`SKIP_BUILD=1 bash tests/e2e/web_{documents,search,tenancy,auth}/run.sh`).
- **M09 F1 shell** merged (tokens, self-hosted Inter, brand SVGs from `design-reference/`, AppShell, shared UI primitives; vitest 518 pass, lint/typecheck/build clean; e2e suites not run). Placeholder routes for Integrations/Activity/People/Settings behind `SHOW_REDESIGN_PLACEHOLDERS`.

## Next steps
1. Docker back → `bash scripts/dev/verify.sh`, e2e suites, acceptance run; fix small breakage; record results in `docs/release/CHECKLIST.md`.
2. Merge M08 security + operations; run their suites; then tag `baseline-m08` locally per `docs/release/BASELINE.md` (NO GitHub Release/deploy); update PROJECT_STATUS (M08 → 108/108 incl. all 12), TEST_DEBT; push.
3. **M09 F2–F5**: four parallel tracks after F1 (owners per `docs/architecture/m09-contracts.md`): A Search+Library, B Chat+Evidence (persistent evidence panel), C Graph+Research+Memory, D Org/Team/Settings/Integrations/Activity; small read-only backend endpoints approved: workspace overview + ingestion jobs list(+counts); usage = disabled panel; invitations deferred. Feature pages must not add `<main>`/page padding; use `PageHeader`. Then user reviews in a real browser (nobody has seen the UI yet).
4. M10 planning/approval (free VM), only on the user's go-ahead.

## Open asks for the user
Restart Docker Desktop + move its disk to D:; clear the Cowork image (Settings → Storage); browser feedback on M07 screens and the new shell; sample-corpus decision; Retry button on `unsupported` files.

## Reminders
psycopg tests/mypy only via `scripts/dev/linux-runner/run.sh` (set `MYPYPATH` inside the quoted command); `bash scripts/dev/verify.sh` is the quiet check; never touch compose project `ukos` from agents; live smoke: `GRAPH=1 M06=1 M07=1 node tests/e2e/smoke_dev/smoke.mjs` (warm LM Studio first: first call can time out).
