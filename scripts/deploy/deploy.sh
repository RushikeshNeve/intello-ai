#!/usr/bin/env bash
# Deploy (or update) the production stack on the VM. UNTESTED on a real VM.
#   bash scripts/deploy/deploy.sh [--no-pull] [--tag TAG]
# Steps: preflight -> git pull (fast-forward only) -> build images tagged ukos-*:TAG (TAG = git short sha) ->
# pull third-party images -> start data services -> api-migrate -> bucket init -> start everything -> wait healthy.
# The previously live tag is kept (images are not pruned) and recorded for scripts/deploy/rollback.sh.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_env_file
PULL=1; TAG=""
while [[ $# -gt 0 ]]; do case "$1" in
  --no-pull) PULL=0; shift;; --tag) TAG="$2"; shift 2;; *) die "unknown argument $1";; esac; done

cd "$DEPLOY_ROOT"
log "preflight"; bash scripts/deploy/preflight.sh >&2

if ((PULL)); then log "git pull --ff-only"; git pull --ff-only >&2; fi
TAG="${TAG:-$(git rev-parse --short=12 HEAD)}"
export UKOS_IMAGE_TAG="$TAG"
PREV="$(cat "$STATE_DIR/current_tag" 2>/dev/null || true)"

# Snapshot DB before a schema change when a stack is already running (safety net; rollback of data is a restore).
if [[ -n "$PREV" && "${SKIP_PRE_BACKUP:-0}" != "1" ]]; then
  log "pre-deploy backup (SKIP_PRE_BACKUP=1 to skip)"
  bash scripts/deploy/backup-cron.sh >&2 || die "pre-deploy backup failed; fix it or set SKIP_PRE_BACKUP=1"
fi

log "build ukos-api / ukos-web as tag $TAG (first build on ARM can take 10+ minutes)"
dc build api web >&2
log "pull third-party images"
dc pull --ignore-buildable >&2 || log "pull reported problems; continuing with local images"

log "data services"
dc up -d --wait postgres redis rushdb objectstore >&2
log "migrations (api-migrate)"
dc run --rm api-migrate >&2
log "bucket init (idempotent)"
dc run --rm objectstore-init >&2
log "start the stack"
dc up -d --remove-orphans >&2
wait_healthy "${HEALTH_WAIT_SECONDS:-420}"

[[ -n "$PREV" && "$PREV" != "$TAG" ]] && echo "$PREV" > "$STATE_DIR/previous_tag"
echo "$TAG" > "$STATE_DIR/current_tag"
log "deployed tag $TAG (previous: ${PREV:-none}). Next: node tests/e2e/prod_smoke/smoke.mjs"
