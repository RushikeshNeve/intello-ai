#!/usr/bin/env bash
# Boots the full stack in the isolated project ukos-m08-b-load (ports in scripts/ops/stack.sh), runs one of:
#   bash tests/load/run.sh load     load + fault injection, writes docs/ops/load-results.md
#   bash tests/load/run.sh trace    API -> outbox -> Celery -> worker trace/log correlation proof
#   bash tests/load/run.sh all
#   KEEP=1 ... leaves the stack up;  PHASES=upload,search bash tests/load/run.sh load  runs a subset
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../../scripts/ops/stack.sh"
MODE="${1:-all}"
PROJECT_NAME=ukos-m08-b-load
stack_env "$PROJECT_NAME"
export PROJECT="$PROJECT_NAME" WEB_URL="$WEB_PUBLIC_URL" RUSHDB_HOST_URL="http://127.0.0.1:${RUSHDB_HTTP_HOST_PORT}" RUSHDB_TOKEN
cleanup() { set +e; if [[ "${KEEP:-0}" != "1" ]]; then stack_down; fi; }
trap cleanup EXIT
if curl -fsS "http://127.0.0.1:${AI_ROUTER_PORT}/health" >/dev/null 2>&1; then ops_die "port ${AI_ROUTER_PORT} already serves something"; fi

stack_down
# The AI stubs are started (and stopped) by the Node scripts themselves; the stack only needs them reachable
# once it is up, so start a temporary router for the health-gated boot is not needed (api/worker do not call at boot).
stack_up

machine() {
  local cpu mem
  cpu="$(powershell.exe -NoProfile -Command "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name + ', ' + (Get-CimInstance Win32_Processor | Select-Object -First 1).NumberOfLogicalProcessors + ' logical processors'" 2>/dev/null | tr -d '\r' || true)"
  mem="$(powershell.exe -NoProfile -Command "[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)" 2>/dev/null | tr -d '\r' || true)"
  echo "- Host: ${cpu:-unknown CPU}, ${mem:-?} GB RAM, Windows 11, Docker Desktop (Linux VM), Node $(node -v); load generator and stack share the machine."
  echo "- Docker: $(docker version --format 'server {{.Server.Version}}' 2>/dev/null || echo unknown); services without CPU/memory limits."
}
export MACHINE_DESC="$(machine)"

STATUS=0
if [[ "$MODE" == "load" || "$MODE" == "all" ]]; then node "$OPS_ROOT/tests/load/load.mjs" || STATUS=1; fi
if [[ "$MODE" == "trace" || "$MODE" == "all" ]]; then node "$OPS_ROOT/tests/load/trace.mjs" || STATUS=1; fi
exit $STATUS
