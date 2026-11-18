#!/usr/bin/env bash
# M02 full-stack e2e (the M02 gate): REAL PostgreSQL, Redis, Dex, RustFS (+ bucket/CORS init), the API (with
# migrations) and the web app, driven over HTTP like a browser: uploads go straight to the object store through
# the presigned URL, everything else through the web BFF.
#
#   bash tests/e2e/fullstack_m02/run.sh           # build, run, tear down
#   KEEP=1 bash tests/e2e/fullstack_m02/run.sh    # leave the stack up (web http://127.0.0.1:33500)
#
# Isolated project `ukos-e2e-m02` and ports outside every other suite's range; never touches the `ukos` dev stack.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && (pwd -W 2>/dev/null || pwd))"
HERE="$ROOT/tests/e2e/fullstack_m02"
PROJECT=ukos-e2e-m02

export MSYS_NO_PATHCONV=1
export WEB_HOST_PORT=33500 API_HOST_PORT=38500 OIDC_HOST_PORT=38580
export POSTGRES_HOST_PORT=35800 REDIS_HOST_PORT=36800 OBJECT_STORE_HOST_PORT=39500
export OIDC_ISSUER_URL="http://127.0.0.1:${OIDC_HOST_PORT}/dex"
export OIDC_REDIRECT_URIS="http://127.0.0.1:${WEB_HOST_PORT}/api/auth/callback"
export WEB_PUBLIC_URL="http://127.0.0.1:${WEB_HOST_PORT}"
export API_CORS_ORIGINS="$WEB_PUBLIC_URL"
export S3_PUBLIC_ENDPOINT_URL="http://127.0.0.1:${OBJECT_STORE_HOST_PORT}"
export UPLOAD_MAX_BYTES=5242880              # 5 MiB, so the oversize case needs no huge body
export ORG_STORAGE_QUOTA_BYTES=20971520      # 20 MiB
export UKOS_ENV=test
export WEB_SESSION_SECRET="$(node -e "console.log(require('crypto').randomBytes(36).toString('base64url'))")"

ENVF="$ROOT/infrastructure/env/.env.example"
COMPOSE=(docker compose -p "$PROJECT" --env-file "$ENVF"
  -f "$ROOT/infrastructure/compose/data.yml"
  -f "$ROOT/infrastructure/compose/identity.yml"
  -f "$ROOT/infrastructure/compose/objects.yml"
  -f "$ROOT/infrastructure/compose/apps.yml"
  -f "$HERE/compose.gate.yaml")

cleanup() {
  set +e
  if [ "${KEEP:-0}" != "1" ]; then
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1
    echo "cleanup: compose project $PROJECT removed"
  else
    echo "KEEP=1: stack left running (remove with: docker compose -p $PROJECT ... down -v)"
  fi
}
trap cleanup EXIT

echo "== build + start ($PROJECT): web :$WEB_HOST_PORT api :$API_HOST_PORT dex :$OIDC_HOST_PORT objects :$OBJECT_STORE_HOST_PORT"
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE[@]}" up -d --build --wait --wait-timeout 600

echo "== tests"
set +e
WEB_URL="$WEB_PUBLIC_URL" STORE_URL="$S3_PUBLIC_ENDPOINT_URL" UPLOAD_MAX_BYTES="$UPLOAD_MAX_BYTES" \
PG_EXEC="docker exec ${PROJECT}-postgres-1" \
  node --test --test-reporter=spec "$HERE/fullstack.test.mjs"
STATUS=$?
set -e

echo "== container logs: secrets, tokens and presigned-URL material"
LOGS="$("${COMPOSE[@]}" logs --no-color api web 2>&1)"
if printf '%s' "$LOGS" | grep -E 'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.|change-me-oidc-client-secret|ukos-dev-(alice|bob|mallory)-password|X-Amz-Signature|x-amz-signature|X-Amz-Credential|change-me-s3-secret-key|'"$WEB_SESSION_SECRET" >/dev/null; then
  echo "FAIL: secret, token or presigned-URL material found in api/web logs" >&2
  STATUS=1
else
  echo "ok: none found in $(printf '%s\n' "$LOGS" | wc -l) log lines"
fi
exit $STATUS
