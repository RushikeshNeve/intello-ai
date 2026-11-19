#!/usr/bin/env bash
# M04.03 web search, chat and evidence e2e: real Dex (compose project ukos-m04-web) + `next start` on the
# host + an in-memory stub of the M04 search, chat (SSE) and evidence API shapes.
#
#   bash tests/e2e/web_search/run.sh            # build, run, tear down
#   SKIP_BUILD=1 bash tests/e2e/web_search/run.sh
#
# Ports: web 39440, stub API + object store 39441, Dex 39442.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && (pwd -W 2>/dev/null || pwd))"
WEB_DIR="$ROOT/apps/web"
HERE="$ROOT/tests/e2e/web_search"
PROJECT=ukos-m04-web
WEB_PORT=39440
STUB_PORT=39441

export MSYS_NO_PATHCONV=1
export OIDC_HOST_PORT=39442
export OIDC_ISSUER_URL="http://127.0.0.1:${OIDC_HOST_PORT}/dex"
export OIDC_REDIRECT_URIS="http://127.0.0.1:${WEB_PORT}/api/auth/callback"

COMPOSE=(docker compose -p "$PROJECT" --env-file "$ROOT/infrastructure/env/.env.example" -f "$ROOT/infrastructure/compose/identity.yml")
WEB_PID=""
STUB_PID=""
LOG_DIR="$(mktemp -d)"

listener_pid() {
  if command -v netstat >/dev/null 2>&1 && netstat -ano >/dev/null 2>&1; then
    netstat -ano 2>/dev/null | awk -v p=":$1" '$2 ~ p"$" && $4 == "LISTENING" {print $5; exit}'
  else
    ss -ltnp "sport = :$1" 2>/dev/null | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1
  fi
}

stop_port() { # Git Bash `kill` does not reliably reach a native node.exe, so stop by the listener's real PID.
  local pid
  pid="$(listener_pid "$1")"
  [ -n "$pid" ] && node -e "try{process.kill(Number(process.argv[1]))}catch{}" "$pid"
  return 0
}

cleanup() {
  set +e
  [ -n "$WEB_PID" ] && kill "$WEB_PID" 2>/dev/null
  [ -n "$STUB_PID" ] && kill "$STUB_PID" 2>/dev/null
  stop_port "$WEB_PORT"
  stop_port "$STUB_PORT"
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1
  local left=""
  for p in "$WEB_PORT" "$STUB_PORT"; do [ -n "$(listener_pid "$p")" ] && left="$left $p"; done
  echo "cleanup: compose project $PROJECT removed; node servers stopped${left:+ (STILL LISTENING:$left)} (logs: $LOG_DIR)"
}

for p in "$WEB_PORT" "$STUB_PORT" "$OIDC_HOST_PORT"; do
  if [ -n "$(listener_pid "$p")" ]; then
    echo "port $p is already in use (pid $(listener_pid "$p")); refusing to run" >&2
    exit 2
  fi
done
trap cleanup EXIT

wait_http() { # url, seconds
  for _ in $(seq 1 "$2"); do
    if curl -fsS "$1" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "timeout waiting for $1" >&2
  return 1
}

echo "== Dex ($PROJECT, :$OIDC_HOST_PORT)"
"${COMPOSE[@]}" up -d --wait

if [ -z "${SKIP_BUILD:-}" ]; then
  echo "== next build"
  (cd "$WEB_DIR" && node node_modules/next/dist/bin/next build >"$LOG_DIR/build.log" 2>&1) || { cat "$LOG_DIR/build.log"; exit 1; }
fi

echo "== stub search+chat+evidence API + object store (:$STUB_PORT)"
STUB_PORT=$STUB_PORT OIDC_ISSUER_URL="$OIDC_ISSUER_URL" node "$HERE/stub-api.mjs" >"$LOG_DIR/stub.log" 2>&1 &
STUB_PID=$!

echo "== web server (:$WEB_PORT)"
SESSION_SECRET="$(node -e "console.log(require('crypto').randomBytes(36).toString('base64url'))")"
(
  cd "$WEB_DIR"
  # Test values only: the client secret is the .env.example placeholder Dex was started with.
  export UKOS_ENV=test
  export OIDC_INTERNAL_URL="$OIDC_ISSUER_URL"
  export OIDC_CLIENT_ID=ukos-web
  export OIDC_CLIENT_SECRET=change-me-oidc-client-secret
  export OIDC_AUDIENCE=ukos-api
  export WEB_PUBLIC_URL="http://127.0.0.1:${WEB_PORT}"
  export WEB_SESSION_SECRET="$SESSION_SECRET"
  export API_INTERNAL_BASE_URL="http://127.0.0.1:${STUB_PORT}"
  exec node node_modules/next/dist/bin/next start -p "$WEB_PORT" -H 127.0.0.1
) >"$LOG_DIR/web.log" 2>&1 &
WEB_PID=$!

wait_http "http://127.0.0.1:${STUB_PORT}/__log" 30
wait_http "http://127.0.0.1:${WEB_PORT}/api/health" 60

echo "== tests"
set +e
WEB_URL="http://127.0.0.1:${WEB_PORT}" STUB_URL="http://127.0.0.1:${STUB_PORT}" OIDC_ISSUER_URL="$OIDC_ISSUER_URL" \
  node --test --test-reporter=spec "$HERE/web_search.test.mjs"
STATUS=$?
set -e

echo "== web server log: secrets/tokens check"
if grep -E 'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.|change-me-oidc-client-secret|ukos-dev-[a-z]+-password|'"$SESSION_SECRET" "$LOG_DIR/web.log"; then
  echo "FAIL: secret or token material found in the web server log" >&2
  STATUS=1
else
  echo "ok: no tokens, client secret, passwords or session secret in $(wc -l <"$LOG_DIR/web.log") log lines"
fi
exit $STATUS
