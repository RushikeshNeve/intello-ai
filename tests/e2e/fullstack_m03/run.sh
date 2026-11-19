#!/usr/bin/env bash
# M03 full-stack e2e (the M03 gate, also task M03.03.05 crash-injection): REAL PostgreSQL, Redis, Dex, RustFS
# (+ bucket/CORS init), RushDB, `api-migrate`, the API, the web app and the NEW `worker` and `dispatcher`
# containers, driven over HTTP like a browser. The embedding server is the fault-injecting stub on the host
# (tests/e2e/support/embedding-stub.mjs), reached from the containers as host.docker.internal.
#
#   bash tests/e2e/fullstack_m03/run.sh           # build, run, tear down
#   KEEP=1 bash tests/e2e/fullstack_m03/run.sh    # leave the stack up (web http://127.0.0.1:33600)
#   GATE_ONLY="scenario 6" bash ...               # node --test name pattern
#
# Isolated project `ukos-e2e-m03` and ports outside every other suite's range; never touches the `ukos` dev stack.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && (pwd -W 2>/dev/null || pwd))"
HERE="$ROOT/tests/e2e/fullstack_m03"
PROJECT=ukos-e2e-m03

export MSYS_NO_PATHCONV=1
export WEB_HOST_PORT=33600 API_HOST_PORT=38600 OIDC_HOST_PORT=38680
export POSTGRES_HOST_PORT=35900 REDIS_HOST_PORT=36900 OBJECT_STORE_HOST_PORT=39600 RUSHDB_HTTP_HOST_PORT=33610
STUB_PORT=18334
export OIDC_ISSUER_URL="http://127.0.0.1:${OIDC_HOST_PORT}/dex"
export OIDC_REDIRECT_URIS="http://127.0.0.1:${WEB_HOST_PORT}/api/auth/callback"
export WEB_PUBLIC_URL="http://127.0.0.1:${WEB_HOST_PORT}"
export NEXT_PUBLIC_API_BASE_URL="http://127.0.0.1:${API_HOST_PORT}"
export API_CORS_ORIGINS="$WEB_PUBLIC_URL"
export S3_PUBLIC_ENDPOINT_URL="http://127.0.0.1:${OBJECT_STORE_HOST_PORT}"
export AI_BASE_URL="http://host.docker.internal:${STUB_PORT}/v1"
export UKOS_ENV=test
WEB_SESSION_SECRET="$(node -e "console.log(require('crypto').randomBytes(36).toString('base64url'))")"
export WEB_SESSION_SECRET
# Short timers so crash recovery fits in a gate run (heartbeat must be <= lease/2).
export INGEST_LEASE_SECONDS=20 INGEST_HEARTBEAT_SECONDS=5 INGEST_REAP_INTERVAL_SECONDS=5
export INGEST_RETRY_BASE_SECONDS=2 INGEST_MAX_ATTEMPTS=4 INGEST_RECONCILE_INTERVAL_SECONDS=15
export INGEST_CONCURRENCY=2

ENVF="$ROOT/infrastructure/env/.env.example"
COMPOSE=(docker compose -p "$PROJECT" --env-file "$ENVF"
  -f "$ROOT/infrastructure/compose/data.yml"
  -f "$ROOT/infrastructure/compose/identity.yml"
  -f "$ROOT/infrastructure/compose/objects.yml"
  -f "$ROOT/infrastructure/compose/rushdb.yml"
  -f "$ROOT/infrastructure/compose/apps.yml"
  -f "$ROOT/infrastructure/compose/workers.yml"
  -f "$HERE/compose.gate.yaml")

# The embedding stub is started, stopped (connection refused) and restarted by the test itself (scenario 6d),
# so the test owns its lifecycle; this is the safety net if the test process dies without cleaning up.
stop_stub() {
  command -v powershell.exe >/dev/null 2>&1 || return 0
  powershell.exe -NoProfile -Command "Get-NetTCPConnection -LocalPort $STUB_PORT -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force -ErrorAction SilentlyContinue }" >/dev/null 2>&1
}
cleanup() {
  set +e
  stop_stub
  echo "cleanup: embedding stub port $STUB_PORT released"
  if [ "${KEEP:-0}" != "1" ]; then
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1
    echo "cleanup: compose project $PROJECT removed"
  else
    echo "KEEP=1: stack left running (remove with: docker compose -p $PROJECT ... down -v); the stub is stopped"
  fi
}
trap cleanup EXIT

if curl -fsS "http://127.0.0.1:${STUB_PORT}/health" >/dev/null 2>&1; then
  echo "port $STUB_PORT already serves something; refusing to run" >&2; exit 2
fi

echo "== build + start ($PROJECT): web :$WEB_HOST_PORT api :$API_HOST_PORT dex :$OIDC_HOST_PORT objects :$OBJECT_STORE_HOST_PORT rushdb :$RUSHDB_HTTP_HOST_PORT"
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
if ! "${COMPOSE[@]}" up -d --build --wait --wait-timeout 600; then
  echo "== START FAILED: container states and logs" >&2
  "${COMPOSE[@]}" ps -a >&2 || true
  "${COMPOSE[@]}" logs --no-color --tail 60 >&2 || true
  exit 1
fi
"${COMPOSE[@]}" ps

echo "== tests"
set +e
NAME_ARGS=()
if [ -n "${GATE_ONLY:-}" ]; then NAME_ARGS=(--test-name-pattern "$GATE_ONLY"); fi
WEB_URL="$WEB_PUBLIC_URL" STORE_URL="$S3_PUBLIC_ENDPOINT_URL" PROJECT="$PROJECT" \
STUB_PORT="$STUB_PORT" STUB_URL="http://127.0.0.1:${STUB_PORT}" RUSHDB_HOST_URL="http://127.0.0.1:${RUSHDB_HTTP_HOST_PORT}" \
RUSHDB_TOKEN="change-me-rushdb-token" PG_EXEC="docker exec ${PROJECT}-postgres-1" \
INGEST_LEASE_SECONDS="$INGEST_LEASE_SECONDS" INGEST_MAX_ATTEMPTS="$INGEST_MAX_ATTEMPTS" INGEST_CONCURRENCY="$INGEST_CONCURRENCY" \
  node --test --test-reporter=spec --test-timeout=420000 "${NAME_ARGS[@]}" "$HERE/fullstack.test.mjs"
STATUS=$?
set -e

if [ $STATUS -ne 0 ]; then
  echo "== FAILURE: worker/dispatcher/api logs (tail)" >&2
  "${COMPOSE[@]}" logs --no-color --tail 80 worker dispatcher api >&2 || true
fi

echo "== container logs: secrets, tokens, presigned-URL material, document text"
LOGS="$("${COMPOSE[@]}" logs --no-color api web worker dispatcher 2>&1)"
PATTERN='eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.|change-me-oidc-client-secret|ukos-dev-(alice|bob|mallory)-password|X-Amz-Signature|x-amz-signature|X-Amz-Credential|change-me-s3-secret-key|change-me-rushdb-token|change-me-postgres-(worker|admin|app)|change-me-redis|GATE-CANARY|'"$WEB_SESSION_SECRET"
if printf '%s' "$LOGS" | grep -E "$PATTERN" >/dev/null; then
  echo "FAIL: secret, token, presigned-URL material or document text found in container logs" >&2
  printf '%s' "$LOGS" | grep -nE "$PATTERN" | sed -E 's/(.{160}).*/\1.../' | head -10 >&2
  STATUS=1
else
  echo "ok: none found in $(printf '%s\n' "$LOGS" | wc -l) log lines (api, web, worker, dispatcher)"
fi
exit $STATUS
