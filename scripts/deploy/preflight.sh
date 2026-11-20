#!/usr/bin/env bash
# Validates .env.prod before a deploy: required variables, placeholders, lengths, URL consistency, then
# `docker compose config` for the production stack. Prints variable NAMES and problems, never secret values.
#   bash scripts/deploy/preflight.sh                  (ENV_FILE=path to check another file; NO_COMPOSE=1 skips docker)
# Exit 0 = ready, 1 = problems found.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_env_file

fail=0
bad() { printf 'FAIL  %s\n' "$*"; fail=1; }
ok() { printf 'ok    %s\n' "$*"; }

REQUIRED=(PUBLIC_HOST FILES_HOST PUBLIC_BASE_URL ACME_EMAIL UKOS_ENV UKOS_BIND_HOST OIDC_ISSUER_URL OIDC_INTERNAL_URL
  OIDC_REDIRECT_URIS OIDC_CLIENT_ID OIDC_AUDIENCE OIDC_CLIENT_SECRET OIDC_API_CLIENT_SECRET WEB_SESSION_SECRET API_SECRET_KEY
  DEX_ADMIN_EMAIL DEX_ADMIN_USER_ID DEX_ADMIN_PASSWORD_HASH_B64
  POSTGRES_DB POSTGRES_USER POSTGRES_APP_USER POSTGRES_WORKER_USER POSTGRES_PASSWORD POSTGRES_APP_PASSWORD POSTGRES_WORKER_PASSWORD
  DATABASE_URL DATABASE_MIGRATION_URL DATABASE_WORKER_URL REDIS_PASSWORD REDIS_URL CELERY_BROKER_URL WEB_REDIS_URL
  RUSHDB_TOKEN S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY S3_PUBLIC_ENDPOINT_URL AI_BASE_URL AI_API_KEY AI_EMBEDDING_MODEL AI_EMBEDDING_DIMENSIONS)
# name:minimum length
SECRETS=(OIDC_CLIENT_SECRET:32 OIDC_API_CLIENT_SECRET:32 WEB_SESSION_SECRET:32 API_SECRET_KEY:32 POSTGRES_PASSWORD:32
  POSTGRES_APP_PASSWORD:32 POSTGRES_WORKER_PASSWORD:32 REDIS_PASSWORD:32 RUSHDB_TOKEN:32 S3_ACCESS_KEY_ID:16 S3_SECRET_ACCESS_KEY:32
  DEX_ADMIN_PASSWORD_HASH_B64:60 AI_API_KEY:16)

for k in "${REQUIRED[@]}"; do
  v="$(env_get "$k")"
  if [[ -z "$v" ]]; then bad "$k is empty or missing"
  elif [[ "$v" == *CHANGE_ME* ]]; then bad "$k still has a CHANGE_ME placeholder"; fi
done

declare -A seen=()
for spec in "${SECRETS[@]}"; do
  k="${spec%%:*}"; min="${spec##*:}"; v="$(env_get "$k")"
  [[ -z "$v" || "$v" == *CHANGE_ME* ]] && continue
  ((${#v} >= min)) || bad "$k is shorter than $min characters"
  [[ "$v" =~ [[:space:]\"\'\$\#] ]] && bad "$k contains a space, quote, \$ or # (regenerate with the documented command)"
  # distinct secrets (a reused value would let one leak open another)
  case "$k" in AI_API_KEY|DEX_ADMIN_PASSWORD_HASH_B64) ;; *)
    if [[ -n "${seen[$v]:-}" ]]; then bad "$k has the same value as ${seen[$v]}"; else seen[$v]="$k"; fi;; esac
done

host="$(env_get PUBLIC_HOST)"; files="$(env_get FILES_HOST)"
[[ "$(env_get PUBLIC_BASE_URL)" == "https://$host" ]] || bad "PUBLIC_BASE_URL must equal https://\$PUBLIC_HOST"
[[ "$host" != "$files" ]] || bad "PUBLIC_HOST and FILES_HOST must differ"
for h in "$host" "$files"; do
  [[ "$h" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || bad "host '$h' is not a valid public DNS name"
  [[ "$h" =~ (localhost|127\.0\.0\.1|example\.(org|com|net)$) ]] && bad "host '$h' is a placeholder/local name"
done
[[ "$(env_get S3_PUBLIC_ENDPOINT_URL)" == "https://$files" ]] || bad "S3_PUBLIC_ENDPOINT_URL must equal https://\$FILES_HOST"
[[ "$(env_get OIDC_ISSUER_URL)" == "https://$host/dex" ]] || bad "OIDC_ISSUER_URL must equal https://\$PUBLIC_HOST/dex"
[[ "$(env_get UKOS_ENV)" == "production" ]] || bad "UKOS_ENV must be production"
[[ "$(env_get UKOS_BIND_HOST)" == "127.0.0.1" ]] || bad "UKOS_BIND_HOST must be 127.0.0.1 (only Caddy is public)"
[[ "$(env_get AI_BASE_URL)" == https://* ]] || bad "AI_BASE_URL must be https (hosted provider)"
[[ "$(env_get DEX_ADMIN_EMAIL)" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || bad "DEX_ADMIN_EMAIL is not an email"
[[ "$(env_get DEX_ADMIN_USER_ID)" =~ ^[0-9a-fA-F-]{36}$ ]] || bad "DEX_ADMIN_USER_ID must be a UUID"
if [[ -n "$(env_get DEX_ADMIN_PASSWORD_HASH_B64)" ]] && ! printf '%s' "$(env_get DEX_ADMIN_PASSWORD_HASH_B64)" | base64 -d 2>/dev/null | grep -qE '^\$2[aby]\$'; then
  bad "DEX_ADMIN_PASSWORD_HASH_B64 does not decode to a bcrypt hash"
fi
[[ -z "$(env_get CONNECTOR_ENCRYPTION_KEY)" ]] && printf 'note  CONNECTOR_ENCRYPTION_KEY empty: connectors with stored secrets stay disabled\n'

if ((fail)); then printf '\npreflight: PROBLEMS FOUND in %s\n' "${ENV_FILE##*/}"; exit 1; fi
ok "variables, secrets, URLs"

if [[ "${NO_COMPOSE:-0}" == "1" ]]; then printf 'skip  docker compose config (NO_COMPOSE=1)\n'; exit 0; fi
command -v docker >/dev/null || { printf 'FAIL  docker not found\n'; exit 1; }
if out="$(dc config -q 2>&1)"; then ok "docker compose config (prod stack)"; else
  printf 'FAIL  docker compose config:\n%s\n' "$out" | sed -E 's#(://[^:/@]+:)[^@/]+@#\1***@#g'; exit 1; fi
printf '\npreflight: OK\n'
