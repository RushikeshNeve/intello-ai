# Agent rules (read this instead of a long brief)

**Host (Windows 11, Git Bash, `MSYS_NO_PATHCONV=1`).** Application Control blocks psycopg and mypy on the host.
- psycopg tests and mypy: `bash scripts/dev/linux-runner/run.sh <dir> "<cmd>"` (runbook §6). Set `MYPYPATH=...` INSIDE the quoted command. Project and ports go as env on the run.sh line (`UKOS_TEST_COMPOSE_PROJECT=... POSTGRES_HOST_PORT=... REDIS_HOST_PORT=...`).
- ruff on host: `cd apps/api && UV_PROJECT_ENVIRONMENT="C:/Users/RUSHIK~1/AppData/Local/ukos/venvs/api" uv run --no-sync python -m ruff check . && ... format --check .`. Repo tests: `--config ../../tests/ruff.toml`.
- `python -m` only. Write code with Write/Edit (shell mangles escapes). No new dependencies unless unavoidable.
- Docker: never touch project `ukos` (the user's stack, ports 3300/8300/8380/3310/9300/5532/6479). Use your own project/ports; `down -v` at the end.
- Commits end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. No fabricated results.

**Token budget (the user's weekly limit is tight).**
- Test what the acceptance line needs: happy path, the main failure modes, one cross-tenant check per new route/table. NO exhaustive matrices, NO hypothesis fuzzing, at most ONE mutation check per task.
- Run only the suites your change can affect; the coordinator runs the full suite once at merge.
- Read only the files you need; do not re-read files you just wrote; keep command output short (`| tail -3`).
- Handoff report: at most 25 lines: branch + commit, files, test counts, deviations from the contract, anything the next agent must know. No tables of every test.
