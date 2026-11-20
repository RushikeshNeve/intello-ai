#!/usr/bin/env bash
# Shared helpers for scripts/deploy/*.sh (sourced). UNTESTED on a real VM; written for Linux (Ubuntu) + Docker Compose v2.24+.
DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ENV_FILE:-$DEPLOY_ROOT/.env.prod}"
PROJECT="${COMPOSE_PROJECT_NAME_OVERRIDE:-ukos-prod}"
CF="$DEPLOY_ROOT/infrastructure/compose"
COMPOSE_FILES=(-f "$CF/data.yml" -f "$CF/rushdb.yml" -f "$CF/objects.yml" -f "$CF/identity.yml" -f "$CF/apps.yml" -f "$CF/workers.yml" -f "$CF/prod.yml")

log() { printf '\n==> %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# dc <compose args...> : the production stack, always with the prod env file.
dc() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" "$@"; }

# env_get KEY : value of KEY in the env file with ${VAR} references expanded like docker compose does
# (never printed by these scripts for secrets).
env_get() {
  local v re='\$\{([A-Za-z0-9_]+)\}' n=0 name
  v="$(grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d $'\r')"
  while [[ "$v" =~ $re && $n -lt 5 ]]; do
    name="${BASH_REMATCH[1]}"; v="${v//\$\{$name\}/$(env_get "$name")}"; n=$((n + 1))
  done
  printf '%s' "$v"
}

require_env_file() { [[ -f "$ENV_FILE" ]] || die "env file not found: $ENV_FILE (copy infrastructure/env/.env.prod.example)"; }

# Records which image tag is live, so rollback knows the previous one.
STATE_DIR="${DEPLOY_STATE_DIR:-$DEPLOY_ROOT/.data/deploy}"
mkdir -p "$STATE_DIR"

# wait_healthy <seconds> : waits until every non-one-shot service reports healthy/running.
wait_healthy() {
  local deadline=$((SECONDS + ${1:-300})) bad
  while ((SECONDS < deadline)); do
    bad="$(dc ps --format '{{.Service}} {{.Health}} {{.State}}' | awk '$3!="running" || ($2!="" && $2!="healthy"){print $1}' | tr '\n' ' ')"
    [[ -z "$bad" ]] && return 0
    sleep 5
  done
  die "services not healthy after ${1:-300}s: $bad"
}
