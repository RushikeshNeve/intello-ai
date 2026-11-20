#!/usr/bin/env bash
# M08.02.02 restore rehearsal / disaster recovery. Restores a backup made by backup.sh into a FRESH compose
# project (refuses a project that already has volumes, unless --force) and verifies it automatically:
#   1 manifest: every file and object sha256 matches (and no .env in the backup)
#   2 fresh volumes; rushdb volume <- cold snapshot; stores up; buckets/CORS created by objectstore-init
#   3 migrations run on the empty database (creates the roles + schema of THIS release); its head must equal
#     the backup's migration head, else abort (restore with the release that wrote the backup)
#   4 pg_restore --clean --if-exists --no-owner; objects synced back into the buckets
#   5 verification: migration head, exact row counts of every table, object checksums (re-export of the
#     restored buckets), RushDB chunk counts per tenant, text + vector indexes ready, BM25 probe hits identical
#   6 --start-app: brings up the rest of the stack (api, web, worker, dispatcher, dex)
# The app-level check (search + citation + graph query) lives in scripts/ops/verify-app.mjs; rehearsal.sh runs it.
#
#   OPS_PROJECT=ukos-restore bash scripts/ops/restore.sh <backup-dir> [--start-app] [--force]
# Uses the root compose (compose.yaml + .env) unless the caller sourced scripts/ops/stack.sh (rehearsal, tests).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

BACKUP=""; START_APP=0; FORCE=0
while [[ $# -gt 0 ]]; do case "$1" in
  --start-app) START_APP=1; shift;; --force) FORCE=1; shift;; -*) ops_die "unknown argument $1";;
  *) BACKUP="$1"; shift;; esac; done
[[ -n "$BACKUP" && -f "$BACKUP/manifest.json" ]] || ops_die "usage: restore.sh <backup-dir> [--start-app] [--force]"
BACKUP="$(cd "$BACKUP" && (pwd -W 2>/dev/null || pwd))"
: "${OPS_PROJECT:?set OPS_PROJECT to the NEW compose project name}"
# STACK_MODE=1 (rehearsal, tests): the isolated full-stack definition of scripts/ops/stack.sh (ports, env, gate override).
if [[ "${STACK_MODE:-0}" == "1" ]]; then source "$OPS_ROOT/scripts/ops/stack.sh"; stack_env "$OPS_PROJECT"; fi
ops_init "$OPS_PROJECT"
if [[ "${STACK_MODE:-0}" == "1" ]]; then dc() { "${STACK_COMPOSE[@]}" "$@"; }
else dc() { docker compose -p "$OPS_PROJECT" --project-directory "$OPS_ROOT" --env-file "$ENV_FILE" "$@"; }; fi
WORK="$(host_path "$(mktemp -d)")"; trap 'rm -rf "$WORK"' EXIT
mj() { node "$OPS_ROOT/scripts/ops/manifest.mjs" "$@"; }
want() { node -e "const m=JSON.parse(require('fs').readFileSync(process.argv[1],'utf8'));console.log(eval('m.'+process.argv[2]))" "$BACKUP/manifest.json" "$1"; }

existing="$(docker volume ls -q --filter "label=com.docker.compose.project=$OPS_PROJECT" | wc -l | tr -d ' ')"
if [[ "$existing" != "0" && "$FORCE" != "1" ]]; then ops_die "project $OPS_PROJECT already has $existing volume(s); restore into a fresh project (or --force)"; fi

ops_log "1/6 verify the backup manifest"
mj verify "$BACKUP"
EXPECT_HEAD="$(want versions.migration_head)"

ops_log "2/6 fresh volumes, rushdb snapshot, stores, buckets"
dc create postgres rushdb objectstore >/dev/null
docker run --rm -v "${OPS_PROJECT}_rushdb-data:/dst" -v "$(host_path "$BACKUP"):/backup:ro" "$HELPER_IMAGE" \
  sh -c "tar -C /dst -xzf /backup/rushdb-data.tgz"
dc up -d --wait postgres rushdb objectstore >/dev/null
dc run --rm objectstore-init >/dev/null

ops_log "3/6 migrations on the empty database (roles + schema), head must be $EXPECT_HEAD"
dc run --rm api-migrate >&2
HEAD_NOW="$(ops_psql -c 'SELECT version_num FROM alembic_version')"
[[ "$HEAD_NOW" == "$EXPECT_HEAD" ]] || ops_die "this release migrates to $HEAD_NOW but the backup is at $EXPECT_HEAD: restore with the matching release"

ops_log "4/6 pg_restore + object upload"
set +e
dc exec -T postgres pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner < "$BACKUP/postgres.dump" 2> "$WORK/pg_restore.err"
set -e
echo "pg_restore: $(grep -c 'error:' "$WORK/pg_restore.err" || true) error line(s) (tolerated: the verification below is the judge)" >&2
head -5 "$WORK/pg_restore.err" >&2 || true
for b in $BUCKETS; do ops_aws "$BACKUP/objects" s3 sync "/data/$b" "s3://$b" --only-show-errors >&2; done

ops_log "5/6 verification"
FAIL=0
HEAD_RESTORED="$(ops_psql -c 'SELECT version_num FROM alembic_version')"
if [[ "$HEAD_RESTORED" == "$EXPECT_HEAD" ]]; then echo "migration head ok: $HEAD_RESTORED"; else echo "migration head FAILED: $HEAD_RESTORED != $EXPECT_HEAD"; FAIL=1; fi
ops_rowcounts > "$WORK/rowcounts.txt"
mj verify-rows "$BACKUP" "$WORK/rowcounts.txt" || FAIL=1
mkdir -p "$WORK/export"
for b in $BUCKETS; do mkdir -p "$WORK/export/$b"; ops_aws "$WORK/export" s3 sync "s3://$b" "/data/$b" --only-show-errors >&2; done
mj verify-objects "$BACKUP" "$WORK/export" || FAIL=1
ORGS="$(ops_psql -c "SELECT string_agg(id::text, ',') FROM organizations")"
node "$OPS_ROOT/scripts/ops/rushdb.mjs" snapshot "http://127.0.0.1:${RUSHDB_HTTP_HOST_PORT}" "$RUSHDB_TOKEN" "$ORGS" \
  --probe-from "$BACKUP/rushdb-counts.json" > "$WORK/rushdb.json" || FAIL=1
mj verify-rushdb "$BACKUP" "$WORK/rushdb.json" || FAIL=1
if [[ "$FAIL" != "0" ]]; then ops_die "RESTORE VERIFICATION FAILED"; fi

if [[ "$START_APP" == "1" ]]; then
  ops_log "6/6 start the application services"
  dc up -d --wait >&2
fi
echo "RESTORE VERIFIED: project $OPS_PROJECT restored from $BACKUP" >&2
