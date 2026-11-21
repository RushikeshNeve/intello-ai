#!/usr/bin/env bash
# Reproduce the CI gates (.github/workflows/ci.yml) locally through the Linux runner container (this Windows
# host blocks psycopg and mypy). One line of output per gate; exit 1 if any gate fails.
#
#   bash scripts/ci/run_local.sh              # api, eval, web (docker), compose, secrets
#   bash scripts/ci/run_local.sh api eval     # only the named gates: api eval web compose secrets audit
#
# DB/RLS suites and the acceptance run are NOT here (they are nightly.yml): see docs/release/CI.md.
set -u
export MSYS_NO_PATHCONV=1
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
R=scripts/dev/linux-runner/run.sh
GATES=("$@"); [ ${#GATES[@]} -eq 0 ] && GATES=(api eval web compose secrets)
FAILED=0
run() {  # name, command...
  local name="$1"; shift
  local out; out="$("$@" 2>&1)"; local rc=$?
  if [ $rc -eq 0 ]; then echo "PASS $name"; else echo "FAIL $name (rc=$rc)"; printf '%s\n' "$out" | tail -15; FAILED=1; fi
}
want() { for g in "${GATES[@]}"; do [ "$g" = "$1" ] && return 0; done; return 1; }

if want api; then
  run "api: ruff check + format (blocking)" bash $R apps/api "python -m ruff check . && python -m ruff format --check ."
  # Advisory like in ci.yml: 72 pre-existing findings in tests/ (TEST_DEBT.md). Printed, never fails the run.
  echo "INFO repo tests ruff (advisory): $(bash $R apps/api "python -m ruff check --config ../../tests/ruff.toml ../../tests 2>&1 | tail -3 | head -1")"
  run "api: mypy strict" bash $R apps/api "python -m mypy app tests migrations"
  run "api: unit tests + openapi snapshot" bash $R apps/api "python -m pytest -q -p no:cacheprovider tests"
fi
if want eval; then
  run "eval gate (--require-baseline)" bash $R apps/api "python ../../tests/evaluation/run_eval.py --require-baseline"
  run "eval metrics + latency/cost budgets" bash $R apps/api "python ../../tests/evaluation/run_eval.py --require-baseline --metrics && python ../../tests/evaluation/budgets.py"
  run "prompt change gate" bash -c 'git rev-parse --verify -q origin/main >/dev/null && BASE=origin/main bash scripts/ci/prompt_gate.sh --eval-cmd true || echo "no origin/main: skipped"'
fi
if want web; then
  # Default: Node 24 in a container (node_modules stays inside it). WEB_HOST=1: use the host Node 24 + npm in apps/web
  # (works on this Windows host and is what the 2026-11-20 run used while Docker Desktop was down).
  if [ "${WEB_HOST:-0}" = "1" ]; then
    run "web: lint, typecheck, test, build (host)" bash -c "cd apps/web && npm ci --no-audit --no-fund && npm run lint && npm run typecheck && npm test && NEXT_TELEMETRY_DISABLED=1 npm run build"
  else
  run "web: lint, typecheck, test, build" docker run --rm -v "$(cygpath -m "$PWD" 2>/dev/null || pwd)/apps/web:/src:ro" node:24-alpine sh -c \
    "cp -r /src /w && cd /w && npm ci --no-audit --no-fund && npm run lint && npm run typecheck && npm test && NEXT_TELEMETRY_DISABLED=1 npm run build"
  fi
fi
if want compose; then
  # CI validates against the example env. Locally an existing .env is never touched: we validate with it instead.
  run "docker compose config" bash -c 'if [ -e .env ]; then docker compose config --quiet; else cp infrastructure/env/.env.example .env; docker compose config --quiet; rc=$?; rm -f .env; exit $rc; fi'
fi
if want secrets; then
  run "secret scan" python scripts/ci/secret_scan.py
fi
if want audit; then
  run "audit (non-blocking report; fails on critical)" bash $R apps/api "uv export --frozen --no-dev --no-hashes --no-emit-project -o /tmp/req.txt && (uvx pip-audit -r /tmp/req.txt --no-deps --disable-pip --format json -o /tmp/pa.json || true) && python ../../scripts/ci/audit_gate.py pip /tmp/pa.json"
fi
[ $FAILED -eq 0 ] && echo "ALL GATES PASSED" || echo "SOME GATES FAILED"
exit $FAILED
