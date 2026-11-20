# CI: what runs where, and branch protection

## Workflows (`.github/workflows/`, free GitHub-hosted runners, no secrets, read-only token)
| Workflow | Trigger | Jobs |
|---|---|---|
| `ci.yml` | push to `main`, every pull request | `api` (ruff check+format for app and repo tests, strict mypy, unit tests incl. the OpenAPI snapshot), `eval` (offline evaluation gate `--require-baseline`, report uploaded), `web` (lint, typecheck, vitest, build), `compose-config` (`docker compose config` on the example env), `secret-scan` (`scripts/ci/secret_scan.py`), `audit` (pip-audit + `npm audit --omit=dev`: reported, fails only on critical) |
| `nightly.yml` | 02:17 UTC daily + manual | `db-integration` (matrix over `tests/security` and every `tests/integration/*` suite: the fixtures start their OWN docker compose project with the real role/RLS setup, so they need Docker, not service containers), `acceptance` (`tests/e2e/acceptance/run.sh`) |

Why DB suites are not service containers: `tests/infrastructure/compose_support.py` brings up Postgres with the production init scripts (roles `ukos_app`/`ukos_worker`, NOBYPASSRLS) through docker compose. A plain `postgres:` service would not have those roles. GitHub's Ubuntu runners include Docker, so the same fixtures run unchanged, nightly.

The nightly `acceptance` job assumes the runner reaches host stubs at `host.docker.internal` (a /etc/hosts line is added); this has **not been run on GitHub** (UNTESTED there; the same script passes locally, see `CHECKLIST.md`).
Nothing in either workflow has been executed on GitHub yet: the YAML is validated locally, and every command is reproduced by `scripts/ci/run_local.sh`.

## Reproduce locally
```bash
bash scripts/ci/run_local.sh                 # api eval web compose secrets
bash scripts/ci/run_local.sh api eval        # selected gates (add `audit` for the dependency audit)
```
The web gate runs in a `node:24-alpine` container; Python gates run through `scripts/dev/linux-runner/run.sh` (this Windows host blocks mypy/psycopg).

## OpenAPI snapshot
`apps/api/openapi.snapshot.json` is the reviewed API contract; `apps/api/tests/contract/test_openapi_snapshot.py` fails when a route or schema changes without it. After an intended change:
```bash
bash scripts/dev/linux-runner/run.sh apps/api "python -m tests.contract.test_openapi_snapshot --update"
git diff apps/api/openapi.snapshot.json     # review it, commit it with the change
```

## Branch protection (recommended settings for `main`; apply in GitHub -> Settings -> Branches)
- Require a pull request before merging; at least 1 approval (or none for a solo repo, but keep the PR).
- Require status checks to pass, branch up to date: `api`, `eval`, `web`, `compose-config`, `secret-scan`. (`audit` is advisory unless you want critical findings to block; `nightly` is not a required check.)
- Require linear history; block force pushes and branch deletion; no bypass for administrators.
- Require conversation resolution; dismiss stale approvals on new commits.
- Actions: restrict to GitHub-owned and verified actions; set default `GITHUB_TOKEN` permissions to read-only (the workflows already declare `contents: read`); never add repository secrets for these workflows.
- Tags: protect `baseline-*` and `v*` so a release tag cannot be moved.
