#!/usr/bin/env bash
# Roll the application containers back to the previously deployed image tag. UNTESTED on a real VM.
#   bash scripts/deploy/rollback.sh [TAG]      (default: the tag recorded as previous by deploy.sh)
# Rolls back CODE only (api, worker, dispatcher, web). Database migrations are NOT reverted: a release whose
# migration is not backwards compatible needs a restore from backup (docs/runbooks/backup-restore.md).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_env_file
TAG="${1:-$(cat "$STATE_DIR/previous_tag" 2>/dev/null || true)}"
[[ -n "$TAG" ]] || die "no previous tag recorded; pass one explicitly (docker images 'ukos-api')"
for img in ukos-api ukos-web; do
  docker image inspect "$img:$TAG" >/dev/null 2>&1 || die "image $img:$TAG not found locally (was it pruned?)"
done
CUR="$(cat "$STATE_DIR/current_tag" 2>/dev/null || true)"
export UKOS_IMAGE_TAG="$TAG"
log "rolling back $CUR -> $TAG (no rebuild, no migration)"
dc up -d --no-build --no-deps api worker dispatcher web caddy >&2
wait_healthy "${HEALTH_WAIT_SECONDS:-300}"
[[ -n "$CUR" ]] && echo "$CUR" > "$STATE_DIR/previous_tag"
echo "$TAG" > "$STATE_DIR/current_tag"
log "now running tag $TAG. Re-run the smoke test: node tests/e2e/prod_smoke/smoke.mjs"
