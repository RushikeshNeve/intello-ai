#!/usr/bin/env bash
# M08.02.02 end-to-end restore rehearsal in two isolated compose projects (never `ukos`):
#   seed (src project, full stack) -> backup.sh -> destroy src incl. volumes -> restore.sh into a FRESH project
#   (rst) with automated verification -> start the app -> rehearsal.mjs verify (search + citation + graph).
#   bash scripts/ops/rehearsal.sh            # KEEP=1 leaves the restored stack up
# Same host ports for both projects (sequential), so the OIDC issuer URL, hence the user identities, are identical.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/stack.sh"

SRC=ukos-m08-b-src; RST=ukos-m08-b-rst
WORK="$OPS_ROOT/.data/rehearsal"; rm -rf "$WORK"; mkdir -p "$WORK"
AI_PID=""
cleanup() { set +e; [[ -n "$AI_PID" ]] && kill "$AI_PID" 2>/dev/null
  if [[ "${KEEP:-0}" != "1" ]]; then (stack_env $RST; stack_down); (stack_env $SRC; stack_down); fi; }
trap cleanup EXIT

node "$OPS_ROOT/tests/load/lib/ai-stubs.mjs" >"$WORK/ai.log" 2>&1 & AI_PID=$!
sleep 2

stack_env "$SRC"; stack_down; stack_env "$RST"; stack_down
stack_env "$SRC"
ops_log "SEED: full stack $SRC"
stack_up
export WEB_URL="$WEB_PUBLIC_URL" PROJECT="$SRC"
node "$OPS_ROOT/scripts/ops/rehearsal.mjs" seed "$WORK/state.json"

ops_log "BACKUP"
T0=$(date +%s)
BACKUP_DIR="$(OPS_PROJECT=$SRC bash "$OPS_ROOT/scripts/ops/backup.sh" --out "$WORK/backups" | tail -1)"
echo "backup took $(( $(date +%s) - T0 ))s: $BACKUP_DIR"; ls -la "$BACKUP_DIR" | sed -n 1,12p

ops_log "DISASTER: destroy $SRC including volumes"
stack_down
[[ -z "$(docker volume ls -q --filter "label=com.docker.compose.project=$SRC")" ]] || ops_die "volumes of $SRC survived"

ops_log "RESTORE into the fresh project $RST"
T0=$(date +%s)
OPS_PROJECT=$RST STACK_MODE=1 bash "$OPS_ROOT/scripts/ops/restore.sh" "$BACKUP_DIR" --start-app
echo "restore + verification took $(( $(date +%s) - T0 ))s"

ops_log "APP-LEVEL VERIFICATION on the restored stack"
stack_env "$RST"
export WEB_URL="$WEB_PUBLIC_URL" PROJECT="$RST"
node "$OPS_ROOT/scripts/ops/rehearsal.mjs" verify "$WORK/state.json"
echo "REHEARSAL PASSED"
