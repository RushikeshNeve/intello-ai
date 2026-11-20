# Release checklist (baseline M08)

Every P0 gate with a command and where its result lives. Status values: PASS (with date/source), PENDING (owned by another M08
agent or not run in this task), UNTESTED. Run commands from the repo root in Git Bash (`export MSYS_NO_PATHCONV=1`); DB suites and
mypy go through `bash scripts/dev/linux-runner/run.sh` (host constraint). Update the "Latest result" column when you rerun.

| # | P0 gate | Command | Latest result / evidence |
|---|---|---|---|
| 1 | API lint, strict mypy, unit tests, OpenAPI snapshot | `bash scripts/ci/run_local.sh api` | see "M08.03 run results" below |
| 2 | Evaluation gate vs committed baseline | `bash scripts/ci/run_local.sh eval` | see below |
| 3 | Web lint, typecheck, test, build | `bash scripts/ci/run_local.sh web` | see below |
| 4 | Compose config validates; secret scan clean | `bash scripts/ci/run_local.sh compose secrets` | see below |
| 5 | Dependency audit (critical blocks) | `bash scripts/ci/run_local.sh audit` | see below |
| 6 | Full-stack acceptance (9 flows, own project, stubs) | `bash tests/e2e/acceptance/run.sh` | see below |
| 7 | M01 tenancy/RLS integration + security suite | `UKOS_TEST_COMPOSE_PROJECT=ukos-v-rls POSTGRES_HOST_PORT=35981 REDIS_HOST_PORT=36981 bash scripts/dev/linux-runner/run.sh apps/api "python -m pytest -q -p no:cacheprovider ../../tests/security ../../tests/integration/m01_tenancy"` | PASS at each milestone gate (`docs/execution/PROJECT_STATUS.md`); rerun by coordinator at merge. M08 agent A adds adversarial cases in `tests/security/**` (PENDING merge) |
| 8 | M02 documents, M03 ingestion (full-stack, crash injection) | `bash tests/e2e/fullstack_m03/run.sh` | PASS 2026-11-19, 17/17 scenarios (PROJECT_STATUS) |
| 9 | M04 chat/search, M05 graph, M06 connectors, M07 memory/research on the live stack | `node tests/e2e/smoke_dev/smoke.mjs` with `GRAPH=1 M07=1 M06=1` (needs LM Studio; M06 needs internet) | PASS per milestone (PROJECT_STATUS, M04-M07 gates) |
| 10 | Per-milestone DB integration suites | `bash scripts/dev/verify.sh db <suite>` for each `tests/integration/*` | PASS at each gate; nightly matrix in `nightly.yml` (UNTESTED on GitHub) |
| 11 | Backup automation + restore rehearsal | see `scripts/ops/README.md` (M08.02, agent B); developer smoke `bash scripts/restore_smoke.sh` | PENDING merge of `scripts/ops/`; smoke PASS (M00.03.05) |
| 12 | Minimal load test; tracing/observability profile | see `tests/load/` and `docs/ops/` (M08.02, agent B) | PENDING merge |
| 13 | Adversarial, deletion-lineage and credential tests | see `tests/security/` (M08.01, agent A) | PENDING merge |
| 14 | Runbooks exist and commands are marked tested/untested | `ls docs/runbooks` (install, operations, security-incident, backup-restore, troubleshooting) | DONE; UNTESTED items are marked [U] in each file |
| 15 | No secrets in the repo; `.env` ignored | `python scripts/ci/secret_scan.py`; `git check-ignore .env` | see row 4 |
| 16 | Baseline document and tag | `docs/release/BASELINE.md`; tag command at its end (coordinator, local only) | document DONE; tag created by the coordinator after merge |
| 17 | No release/deployment performed | `git tag --list` shows only `baseline-*`; no GitHub Release | by policy (REMAINING_PLAN.md) |

Known gaps accepted for the baseline: `docs/execution/TEST_DEBT.md` (thorough testing pass), `docs/execution/POST_M09_BACKLOG.md`.
CI workflows (`.github/workflows/`) have not run on GitHub yet; branch-protection settings: `docs/release/CI.md`.

## M08.03 run results
(Filled from the commands above on 2026-11-20; see the handoff for the exact output.)
