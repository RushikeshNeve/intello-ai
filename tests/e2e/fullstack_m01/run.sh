#!/usr/bin/env bash
# M01 full-stack e2e (coordinator): REAL containers for PostgreSQL, Redis, Dex, the API (with
# api-migrate) and the web app, driven over HTTP like a browser (alice / bob / mallory).
#
#   bash tests/e2e/fullstack_m01/run.sh          # build, run, tear down
#   KEEP=1 bash tests/e2e/fullstack_m01/run.sh   # leave the stack up for inspection
#
# Isolated project `ukos-e2e-m01` and ports outside every other suite's range.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && (pwd -W 2>/dev/null || pwd))"
HERE="$ROOT/tests/e2e/fullstack_m01"
PROJECT=ukos-e2e-m01

export MSYS_NO_PATHCONV=1
export WEB_HOST_PORT=33400 API_HOST_PORT=38400 OIDC_HOST_PORT=38480
export POSTGRES_HOST_PORT=35700 REDIS_HOST_PORT=36700
export OIDC_ISSUER_URL="http://127.0.0.1:${OIDC_HOST_PORT}/dex"
export OIDC_REDIRECT_URIS="http://127.0.0.1:${WEB_HOST_PORT}/api/auth/callback"
export WEB_PUBLIC_URL="http://127.0.0.1:${WEB_HOST_PORT}"
export API_CORS_ORIGINS="$WEB_PUBLIC_URL"
export UKOS_ENV=test
export WEB_SESSION_SECRET="$(node -e "console.log(require('crypto').randomBytes(36).toString('base64url'))")"

COMPOSE=(docker compose -p "$PROJECT" --env-file "$ROOT/infrastructure/env/.env.example"
  -f "$ROOT/infrastructure/compose/data.yml"
  -f "$ROOT/infrastructure/compose/identity.yml"
  -f "$ROOT/infrastructure/compose/apps.yml")

cleanup() {
  set +e
  if [ "${KEEP:-0}" != "1" ]; then
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1
    echo "cleanup: compose project $PROJECT removed"
  else
    echo "KEEP=1: stack left running (docker compose -p $PROJECT ... down -v to remove)"
  fi
}
trap cleanup EXIT

echo "== build + start ($PROJECT): web :$WEB_HOST_PORT, api :$API_HOST_PORT, dex :$OIDC_HOST_PORT"
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE[@]}" up -d --build --wait --wait-timeout 420

echo "== tests"
set +e
WEB_URL="$WEB_PUBLIC_URL" API_URL="http://127.0.0.1:${API_HOST_PORT}" OIDC_ISSUER_URL="$OIDC_ISSUER_URL" \
PG_EXEC="docker exec ${PROJECT}-postgres-1" \
  node --test --test-reporter=spec "$HERE/fullstack.test.mjs"
STATUS=$?
set -e

echo "== container logs: secrets/tokens check"
LOGS="$("${COMPOSE[@]}" logs --no-color api web 2>&1)"
if printf '%s' "$LOGS" | grep -E 'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.|change-me-oidc-client-secret|ukos-dev-(alice|bob|mallory)-password|'"$WEB_SESSION_SECRET" >/dev/null; then
  echo "FAIL: secret or token material found in api/web logs" >&2
  STATUS=1
else
  echo "ok: no tokens, client secret, passwords or session secret in $(printf '%s\n' "$LOGS" | wc -l) log lines"
fi
exit $STATUS
