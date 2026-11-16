#!/usr/bin/env bash
# =============================================================================
# M00.03.05 — Backup/restore smoke test
# Acceptance: "Restored data matches fixture checksums."
#
# Seeds PostgreSQL, RushDB and the object store in an ISOLATED compose project,
# records content digests, backs everything up (with a SHA-256 manifest),
# destroys the project including volumes, restores into fresh volumes, and
# verifies the restored contents produce identical digests.
#
#   scripts/restore_smoke.sh            # run and clean up
#   KEEP=1 scripts/restore_smoke.sh     # keep the restored stack for inspection
#
# Safety: only ever touches the compose project "$SMOKE_PROJECT" (default
# ukos-restore-smoke) and its own volumes. Refuses to run against "ukos".
# Redis is deliberately not backed up: it is a broker, never durable truth (ADR-036).
# =============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Git Bash on Windows: docker.exe / uv.exe need Windows-style paths (D:/...), because
# MSYS_NO_PATHCONV (set below) stops MSYS from translating /d/... for them.
if command -v cygpath >/dev/null 2>&1; then REPO="$(cygpath -m "$REPO")"; fi
SMOKE_PROJECT="${SMOKE_PROJECT:-ukos-restore-smoke}"
ENV_FILE="${ENV_FILE:-$REPO/infrastructure/env/.env.example}"
WORK="${WORK:-$REPO/.data/restore-smoke}"
HELPER_IMAGE="${HELPER_IMAGE:-alpine:3.20}"

if [[ "$SMOKE_PROJECT" == "ukos" || -z "$SMOKE_PROJECT" ]]; then
  echo "refusing to run against project '$SMOKE_PROJECT'" >&2
  exit 2
fi

# Host ports outside the main UKOS block so a running dev stack is never touched.
export POSTGRES_HOST_PORT="${SMOKE_POSTGRES_PORT:-15532}"
export REDIS_HOST_PORT="${SMOKE_REDIS_PORT:-16479}"
export RUSHDB_HTTP_HOST_PORT="${SMOKE_RUSHDB_PORT:-13310}"
export OBJECT_STORE_HOST_PORT="${SMOKE_OBJECTS_PORT:-19300}"

# Git Bash on Windows: stop MSYS from rewriting container paths like /src.
export MSYS_NO_PATHCONV=1
host_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

# Load contract values (credentials, bucket names) for the fixture tool.
set -a
# shellcheck disable=SC1090
source <(grep -E '^[A-Z0-9_]+=' "$ENV_FILE")
set +a
# Re-apply port overrides (the env file must not win).
export POSTGRES_HOST_PORT="${SMOKE_POSTGRES_PORT:-15532}" REDIS_HOST_PORT="${SMOKE_REDIS_PORT:-16479}" \
       RUSHDB_HTTP_HOST_PORT="${SMOKE_RUSHDB_PORT:-13310}" OBJECT_STORE_HOST_PORT="${SMOKE_OBJECTS_PORT:-19300}"

COMPOSE_FILES=(
  -f "$REPO/infrastructure/compose/data.yml"
  -f "$REPO/infrastructure/compose/rushdb.yml"
  -f "$REPO/infrastructure/compose/objects.yml"
)
compose() { docker compose -p "$SMOKE_PROJECT" --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" "$@"; }

export SMOKE_PG_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_HOST_PORT}/${POSTGRES_DB}"
export SMOKE_RUSHDB_URL="http://127.0.0.1:${RUSHDB_HTTP_HOST_PORT}"
export SMOKE_RUSHDB_TOKEN="${RUSHDB_TOKEN}"
export SMOKE_S3_ENDPOINT="http://127.0.0.1:${OBJECT_STORE_HOST_PORT}"
fixtures() {
  uv run --no-project --quiet --with "psycopg[binary]" --with boto3 --with httpx \
    python -I "$REPO/scripts/restore_smoke/fixtures.py" "$@"
}

BACKUP="$WORK/backup"
VOLUMES=(rushdb-data objectstore-data)   # file-level snapshots; Postgres uses pg_dump
log() { printf '\n==> %s\n' "$*"; }
cleanup() { if [[ "${KEEP:-0}" != "1" ]]; then compose down -v --remove-orphans >/dev/null 2>&1 || true; fi; }
trap cleanup EXIT

start_stores() {
  compose up -d --wait postgres rushdb objectstore
  # One-shot bucket bootstrap (idempotent); provided by objects.yml.
  compose run --rm objectstore-init >/dev/null
}

log "1/7 clean slate for project $SMOKE_PROJECT"
compose down -v --remove-orphans >/dev/null 2>&1 || true
rm -rf "$WORK" && mkdir -p "$BACKUP"

log "2/7 start stores and seed fixtures"
start_stores
fixtures seed >/dev/null
fixtures digest > "$WORK/before.json"
cat "$WORK/before.json"

log "3/7 back up (pg_dump + quiesced volume snapshots)"
compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$BACKUP/postgres.dump"
compose stop rushdb objectstore
for vol in "${VOLUMES[@]}"; do
  docker run --rm -v "${SMOKE_PROJECT}_${vol}:/src:ro" -v "$(host_path "$BACKUP"):/backup" "$HELPER_IMAGE" \
    tar -C /src -czf "/backup/${vol}.tgz" .
done
(cd "$BACKUP" && sha256sum postgres.dump ./*.tgz > MANIFEST.sha256)
cat "$BACKUP/MANIFEST.sha256"

log "4/7 destroy project including volumes"
compose down -v --remove-orphans
for vol in "${VOLUMES[@]}"; do
  if docker volume inspect "${SMOKE_PROJECT}_${vol}" >/dev/null 2>&1; then
    echo "volume ${SMOKE_PROJECT}_${vol} still exists after down -v" >&2; exit 1
  fi
done

log "5/7 verify backup manifest"
(cd "$BACKUP" && sha256sum -c MANIFEST.sha256)

log "6/7 restore into fresh volumes"
compose create postgres rushdb objectstore >/dev/null   # creates the named volumes
for vol in "${VOLUMES[@]}"; do
  docker run --rm -v "${SMOKE_PROJECT}_${vol}:/dst" -v "$(host_path "$BACKUP"):/backup:ro" "$HELPER_IMAGE" \
    sh -c "tar -C /dst -xzf /backup/${vol}.tgz"
done
compose up -d --wait postgres
compose exec -T postgres pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner \
  < "$BACKUP/postgres.dump"
compose up -d --wait rushdb objectstore

log "7/7 compare digests"
fixtures digest > "$WORK/after.json"
cat "$WORK/after.json"
uv run --no-project --quiet python -I - "$WORK/before.json" "$WORK/after.json" <<'PY'
import json, sys
before, after = (json.load(open(p, encoding="utf-8")) for p in sys.argv[1:3])
assert before["postgres"]["rows"] > 0 and before["objects"]["objects"] > 0, "fixtures were not seeded"
assert before["objects"]["matches_generator"] and after["objects"]["matches_generator"], "object bytes differ from generator"
problems = [k for k in before if before[k] != after[k]]
if problems:
    print(f"RESTORE SMOKE FAILED: digest mismatch in {problems}")
    sys.exit(1)
print("RESTORE SMOKE PASSED: restored data matches fixture checksums for", ", ".join(sorted(before)))
PY
