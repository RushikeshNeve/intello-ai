#!/usr/bin/env bash
# M08.03.03 acceptance suite: boots a FULL stack (PostgreSQL, Redis, Dex, RustFS, RushDB, api-migrate, api, web,
# worker, dispatcher) in its OWN compose project `ukos-e2e-accept` (ports 336xx/386xx), with the embedding stub and
# the scripted chat stub on the host behind one proxy (ai-stub.mjs), then runs the essential flows over HTTP
# (acceptance.mjs). No LM Studio, no internet, no secrets. Never touches the `ukos` dev stack.
#
#   bash tests/e2e/acceptance/run.sh           # build, run, tear down (about 5-10 min cold)
#   KEEP=1 bash tests/e2e/acceptance/run.sh    # leave the stack up (web http://127.0.0.1:33650)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && (pwd -W 2>/dev/null || pwd))"
HERE="$ROOT/tests/e2e/acceptance"
PROJECT=ukos-e2e-accept

export MSYS_NO_PATHCONV=1
export WEB_HOST_PORT=33650 API_HOST_PORT=38650 OIDC_HOST_PORT=38670
export POSTGRES_HOST_PORT=35950 REDIS_HOST_PORT=36950 OBJECT_STORE_HOST_PORT=39650 RUSHDB_HTTP_HOST_PORT=33660
AI_PORT=18650 EMBED_PORT=18651 CHAT_PORT=18652
export OIDC_ISSUER_URL="http://127.0.0.1:${OIDC_HOST_PORT}/dex"
export OIDC_REDIRECT_URIS="http://127.0.0.1:${WEB_HOST_PORT}/api/auth/callback"
export WEB_PUBLIC_URL="http://127.0.0.1:${WEB_HOST_PORT}"
export NEXT_PUBLIC_API_BASE_URL="http://127.0.0.1:${API_HOST_PORT}"
export API_CORS_ORIGINS="$WEB_PUBLIC_URL"
export S3_PUBLIC_ENDPOINT_URL="http://127.0.0.1:${OBJECT_STORE_HOST_PORT}"
export AI_BASE_URL="http://host.docker.internal:${AI_PORT}/v1"
export UKOS_ENV=test GRAPH_ENABLED=true
# Test-only session secret generated per run (never a real secret).
WEB_SESSION_SECRET="$(node -e "console.log(require('crypto').randomBytes(36).toString('base64url'))")"
export WEB_SESSION_SECRET
# The graph build and the research planner call the chat model; the stub answers instantly.
export INGEST_LEASE_SECONDS=30 INGEST_HEARTBEAT_SECONDS=10 INGEST_REAP_INTERVAL_SECONDS=10 INGEST_RECONCILE_INTERVAL_SECONDS=30

ENVF="$ROOT/infrastructure/env/.env.example"
COMPOSE=(docker compose -p "$PROJECT" --env-file "$ENVF"
  -f "$ROOT/infrastructure/compose/data.yml"
  -f "$ROOT/infrastructure/compose/identity.yml"
  -f "$ROOT/infrastructure/compose/objects.yml"
  -f "$ROOT/infrastructure/compose/rushdb.yml"
  -f "$ROOT/infrastructure/compose/apps.yml"
  -f "$ROOT/infrastructure/compose/workers.yml"
  -f "$HERE/compose.gate.yaml")

PIDS=()
stop_stubs() {
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null; done
  command -v powershell.exe >/dev/null 2>&1 || return 0
  for port in $AI_PORT $EMBED_PORT $CHAT_PORT; do
    powershell.exe -NoProfile -Command "Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force -ErrorAction SilentlyContinue }" >/dev/null 2>&1
  done
}
cleanup() {
  set +e
  stop_stubs
  if [ "${KEEP:-0}" != "1" ]; then
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1
    echo "cleanup: compose project $PROJECT removed, stubs stopped"
  else
    echo "KEEP=1: stack left running (remove: docker compose -p $PROJECT ... down -v)"
  fi
}
trap cleanup EXIT

for port in $AI_PORT $EMBED_PORT $CHAT_PORT; do
  if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then echo "port $port already serves something; refusing to run" >&2; exit 2; fi
done

echo "== stubs: embedding :$EMBED_PORT, scripted chat :$CHAT_PORT, proxy :$AI_PORT"
PORT=$EMBED_PORT DIMENSIONS=768 node "$ROOT/tests/e2e/support/embedding-stub.mjs" >/dev/null 2>&1 & PIDS+=($!)
PORT=$CHAT_PORT CHAT_SCRIPT_FILE="$HERE/chat-script.json" node "$ROOT/tests/e2e/support/chat-stub.mjs" >/dev/null 2>&1 & PIDS+=($!)
PORT=$AI_PORT HOST=0.0.0.0 EMBED_URL="http://127.0.0.1:$EMBED_PORT" CHAT_URL="http://127.0.0.1:$CHAT_PORT" node "$HERE/ai-stub.mjs" >/dev/null 2>&1 & PIDS+=($!)
for port in $EMBED_PORT $CHAT_PORT $AI_PORT; do
  for _ in $(seq 1 50); do curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1 && break; sleep 0.2; done
  curl -fsS "http://127.0.0.1:${port}/health" >/dev/null || { echo "stub on $port did not start" >&2; exit 1; }
done

echo "== build + start ($PROJECT): web :$WEB_HOST_PORT api :$API_HOST_PORT dex :$OIDC_HOST_PORT"
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
if ! "${COMPOSE[@]}" up -d --build --wait --wait-timeout 600; then
  echo "== START FAILED" >&2
  "${COMPOSE[@]}" ps -a >&2 || true
  "${COMPOSE[@]}" logs --no-color --tail 60 >&2 || true
  exit 1
fi

echo "== acceptance flows"
set +e
WEB_URL="$WEB_PUBLIC_URL" node "$HERE/acceptance.mjs"
STATUS=$?
set -e
if [ $STATUS -ne 0 ]; then
  echo "== FAILURE: api/worker/dispatcher logs (tail)" >&2
  "${COMPOSE[@]}" logs --no-color --tail 60 worker dispatcher api >&2 || true
fi
exit $STATUS
