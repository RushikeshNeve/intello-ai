#!/usr/bin/env bash
# Shared helpers for scripts/ops/*.sh (sourced, never executed). Git Bash on Windows and Linux.
# Safety: every script takes the compose project from OPS_PROJECT and refuses the user's dev stack
# `ukos` unless ALLOW_DEV_STACK=1 is set explicitly (backups of the dev stack are legitimate; tests never do it).

OPS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if command -v cygpath >/dev/null 2>&1; then OPS_ROOT="$(cygpath -m "$OPS_ROOT")"; fi
export MSYS_NO_PATHCONV=1
HELPER_IMAGE="${HELPER_IMAGE:-alpine:3.20}"
AWSCLI_IMAGE="${AWSCLI_IMAGE:-amazon/aws-cli:2.40.3}"

ops_log() { printf '\n==> %s\n' "$*" >&2; }
ops_die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
host_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

# ops_init <project> : load the env file (credentials/bucket names only), define compose().
ops_init() {
  OPS_PROJECT="${1:?project name required}"
  if [[ "$OPS_PROJECT" == "ukos" && "${ALLOW_DEV_STACK:-0}" != "1" ]]; then
    ops_die "refusing to touch project 'ukos' (set ALLOW_DEV_STACK=1 to back up the dev stack deliberately)"
  fi
  ENV_FILE="${ENV_FILE:-}"
  if [[ -z "$ENV_FILE" ]]; then
    if [[ -f "$OPS_ROOT/.env" && "$OPS_PROJECT" == "ukos" ]]; then ENV_FILE="$OPS_ROOT/.env"
    else ENV_FILE="$OPS_ROOT/infrastructure/env/.env.example"; fi
  fi
  [[ -f "$ENV_FILE" ]] || ops_die "env file not found: $ENV_FILE"
  # Credentials and bucket names are read for the tools only; they are never written to a backup.
  # Values already in the environment win over the env file (like docker compose itself does).
  local line key
  while IFS= read -r line; do
    key="${line%%=*}"
    # values with ${VAR} are compose-interpolated (prod env): leave them to docker compose, never export them raw
    [[ "$line" == *'${'* ]] && continue
    if [[ -z "${!key+x}" ]]; then export "$key=${line#*=}"; fi
  done < <(grep -E '^[A-Z0-9_]+=' "$ENV_FILE" | tr -d '\r')
  OPS_DATA_FILES=(-f "$OPS_ROOT/infrastructure/compose/data.yml" -f "$OPS_ROOT/infrastructure/compose/rushdb.yml" -f "$OPS_ROOT/infrastructure/compose/objects.yml")
  # Production: scripts/deploy/backup-cron.sh passes the full file list ("-f a -f b ...") so recreated containers keep prod settings.
  if [[ -n "${OPS_COMPOSE_FILES:-}" ]]; then read -r -a OPS_DATA_FILES <<< "$OPS_COMPOSE_FILES"; fi
  BUCKETS="${S3_BUCKET_ORIGINALS:-ukos-originals} ${S3_BUCKET_ARTIFACTS:-ukos-artifacts}"
}

compose() { docker compose -p "$OPS_PROJECT" --env-file "$ENV_FILE" "${OPS_DATA_FILES[@]}" "$@"; }

# psql inside the postgres container as the admin role (RLS does not apply: counts are global).
ops_psql() { compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1 "$@"; }

# Exact row count of every public table as "table=count" lines.
ops_rowcounts() {
  ops_psql -c "SELECT table_name || '=' || (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1"
}

# aws-cli container on the project's network, with a host directory mounted at /data.
ops_aws() {
  local dir="$1"; shift
  docker run --rm --network "${OPS_PROJECT}_default" -v "$(host_path "$dir"):/data" \
    -e AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY_ID" -e AWS_SECRET_ACCESS_KEY="$S3_SECRET_ACCESS_KEY" \
    -e AWS_DEFAULT_REGION="${S3_REGION:-us-east-1}" "$AWSCLI_IMAGE" \
    --endpoint-url http://objectstore:9000 "$@"
}
