#!/usr/bin/env bash
# Boots/stops a FULL isolated stack (postgres, redis, dex, object store, rushdb, api-migrate, api, web, worker,
# dispatcher) in its own compose project, with the AI endpoint pointed at the host stub router
# (tests/load/lib/ai-stubs.mjs). Sourced by rehearsal.sh, tests/load/run.sh and the trace test; modelled on
# tests/e2e/fullstack_m03/run.sh. Never touches the `ukos` dev stack.
#
#   source scripts/ops/stack.sh
#   stack_env ukos-m08-b-load        # exports ports + env, defines STACK_COMPOSE
#   stack_up ; stack_down
# Ports (M08 agent B block): postgres 35982, redis 36982, rushdb 33372, objects 39682, web 33682,
# api 38682, dex 38683, AI stub router 18380 (embedder 18381, chat 18382).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

stack_env() {
  OPS_PROJECT="${1:?project}"
  [[ "$OPS_PROJECT" != "ukos" ]] || ops_die "refusing project 'ukos'"
  export WEB_HOST_PORT=33682 API_HOST_PORT=38682 OIDC_HOST_PORT=38683
  export POSTGRES_HOST_PORT=35982 REDIS_HOST_PORT=36982 OBJECT_STORE_HOST_PORT=39682 RUSHDB_HTTP_HOST_PORT=33372
  export AI_ROUTER_PORT=18380 AI_EMBED_PORT=18381 AI_CHAT_PORT=18382
  export OIDC_ISSUER_URL="http://127.0.0.1:${OIDC_HOST_PORT}/dex"
  export OIDC_REDIRECT_URIS="http://127.0.0.1:${WEB_HOST_PORT}/api/auth/callback"
  export WEB_PUBLIC_URL="http://127.0.0.1:${WEB_HOST_PORT}"
  export NEXT_PUBLIC_API_BASE_URL="http://127.0.0.1:${API_HOST_PORT}"
  export API_CORS_ORIGINS="$WEB_PUBLIC_URL"
  export S3_PUBLIC_ENDPOINT_URL="http://127.0.0.1:${OBJECT_STORE_HOST_PORT}"
  export AI_BASE_URL="http://host.docker.internal:${AI_ROUTER_PORT}/v1"
  export UKOS_ENV=test
  WEB_SESSION_SECRET="${WEB_SESSION_SECRET:-$(node -e "console.log(require('crypto').randomBytes(36).toString('base64url'))")}"
  export WEB_SESSION_SECRET
  # Load tests must measure the system, not the per-user rate limiters (documented in docs/ops/load-results.md).
  export SEARCH_RATE_PER_MIN="${SEARCH_RATE_PER_MIN:-100000}" CHAT_MAX_RUNS_PER_MIN="${CHAT_MAX_RUNS_PER_MIN:-100000}" CHAT_MAX_CONCURRENT_RUNS="${CHAT_MAX_CONCURRENT_RUNS:-50}"
  export INGEST_LEASE_SECONDS="${INGEST_LEASE_SECONDS:-30}" INGEST_HEARTBEAT_SECONDS="${INGEST_HEARTBEAT_SECONDS:-5}" INGEST_REAP_INTERVAL_SECONDS="${INGEST_REAP_INTERVAL_SECONDS:-5}"
  export INGEST_RETRY_BASE_SECONDS="${INGEST_RETRY_BASE_SECONDS:-2}" INGEST_MAX_ATTEMPTS="${INGEST_MAX_ATTEMPTS:-6}" INGEST_RECONCILE_INTERVAL_SECONDS="${INGEST_RECONCILE_INTERVAL_SECONDS:-15}"
  export INGEST_CONCURRENCY="${INGEST_CONCURRENCY:-2}"
  ENV_FILE="$OPS_ROOT/infrastructure/env/.env.example"
  STACK_FILES=(-f "$OPS_ROOT/infrastructure/compose/data.yml" -f "$OPS_ROOT/infrastructure/compose/identity.yml"
    -f "$OPS_ROOT/infrastructure/compose/objects.yml" -f "$OPS_ROOT/infrastructure/compose/rushdb.yml"
    -f "$OPS_ROOT/infrastructure/compose/apps.yml" -f "$OPS_ROOT/infrastructure/compose/workers.yml"
    -f "$OPS_ROOT/tests/e2e/fullstack_m03/compose.gate.yaml")
  # Optional extra compose files (e.g. the observability profile): STACK_EXTRA_FILES="a.yml b.yml"
  for f in ${STACK_EXTRA_FILES:-}; do STACK_FILES+=(-f "$f"); done
  STACK_COMPOSE=(docker compose -p "$OPS_PROJECT" --env-file "$ENV_FILE" "${STACK_FILES[@]}" ${STACK_PROFILES:+--profile $STACK_PROFILES})
  # credentials/bucket names from the env file for the ops tools; everything exported above wins (see ops_init)
  ops_init "$OPS_PROJECT"
}

stack_up() {
  ops_log "build + start ($OPS_PROJECT): web :$WEB_HOST_PORT api :$API_HOST_PORT objects :$OBJECT_STORE_HOST_PORT rushdb :$RUSHDB_HTTP_HOST_PORT"
  "${STACK_COMPOSE[@]}" up -d --build --wait --wait-timeout 600 >&2 || {
    "${STACK_COMPOSE[@]}" ps -a >&2 || true
    "${STACK_COMPOSE[@]}" logs --no-color --tail 40 >&2 || true
    ops_die "stack did not become healthy"; }
}

stack_down() { "${STACK_COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
