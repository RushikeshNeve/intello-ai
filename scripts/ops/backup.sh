#!/usr/bin/env bash
# M08.02.01 backup automation. One consistent backup of a running compose project:
#   postgres.dump        pg_dump -Fc of the whole database (all schemas; --no-owner, roles are recreated
#                        by the init/migrations of a fresh stack; RLS policies and grants are in the dump)
#   rushdb-data.tgz      COLD volume snapshot (DC-009 condition 4): rushdb is stopped, the volume is
#                        tarred, rushdb is started again. Search is unavailable for a few seconds.
#   objects/<bucket>/..  S3-level export of the originals and artifacts buckets (aws s3 sync)
#   manifest.json        versions (migration head, embedding space id, pipeline version, image tags),
#                        row counts, object counts, sha256 of every file; SHA256SUMS for `sha256sum -c`
# M14.08: with BACKUP_ENCRYPTION_KEY_FILE set the payload is encrypted after the manifest is written
#   (scripts/ops/backup-crypto.sh: openssl aes-256-cbc + pbkdf2); manifest.json records encrypted=true + key_id.
# Not included, by design: `.env` / any credential, Redis (a broker, never durable truth, ADR-036), Dex.
#
#   OPS_PROJECT=ukos-m08-b-src bash scripts/ops/backup.sh [--out DIR] [--keep N]
#   --out DIR   backup root (default ./.data/backups)   --keep N  retention: keep the newest N backups
# Prints the backup directory on stdout. Restore: scripts/ops/restore.sh. A dump restores into a stack whose
# migration head is the SAME (the restore refuses a mismatch: run the migrations of that release first).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
source "$(dirname "${BASH_SOURCE[0]}")/backup-crypto.sh"

OUT="$OPS_ROOT/.data/backups"; KEEP=0
while [[ $# -gt 0 ]]; do case "$1" in
  --out) OUT="$2"; shift 2;; --keep) KEEP="$2"; shift 2;; *) ops_die "unknown argument $1";; esac; done
ops_init "${OPS_PROJECT:?set OPS_PROJECT to the compose project name}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT/ukos-backup-$STAMP/objects"
DEST="$(cd "$OUT/ukos-backup-$STAMP" && (pwd -W 2>/dev/null || pwd))"

ops_log "1/4 postgres dump"
compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner > "$DEST/postgres.dump"
MIG_HEAD="$(ops_psql -c 'SELECT version_num FROM alembic_version')"
SPACES="$(ops_psql -c "SELECT coalesce(string_agg(DISTINCT embedding_space_id, ','), '') FROM knowledge_revisions")"
PIPE="$(ops_psql -c "SELECT coalesce(string_agg(DISTINCT pipeline_version, ','), '') FROM knowledge_revisions")"
ops_rowcounts > "$DEST/rowcounts.txt"

ops_log "2/4 object store export (online, S3 level)"
compose up -d --wait objectstore >/dev/null
for b in $BUCKETS; do
  mkdir -p "$DEST/objects/$b"
  ops_aws "$DEST/objects" s3 sync "s3://$b" "/data/$b" --only-show-errors >&2
done

ops_log "3/4 rushdb per-tenant counts, then cold snapshot (stop, tar the volume, start)"
ORGS="$(ops_psql -c "SELECT string_agg(id::text, ',') FROM organizations")"
node "$OPS_ROOT/scripts/ops/rushdb.mjs" snapshot "http://127.0.0.1:${RUSHDB_HTTP_HOST_PORT}" "$RUSHDB_TOKEN" "$ORGS" > "$DEST/rushdb-counts.json"
RUSHDB_WAS_UP="$(compose ps -q rushdb | head -1)"
compose stop rushdb >/dev/null
docker run --rm -v "${OPS_PROJECT}_rushdb-data:/src:ro" -v "$(host_path "$DEST"):/backup" "$HELPER_IMAGE" \
  tar -C /src -czf /backup/rushdb-data.tgz .
if [[ -n "$RUSHDB_WAS_UP" ]]; then compose up -d --wait rushdb >/dev/null; fi

ops_log "4/4 manifest"
node "$OPS_ROOT/scripts/ops/manifest.mjs" write "$DEST" \
  --migration-head "$MIG_HEAD" --embedding-space "$SPACES" --pipeline-version "$PIPE" \
  --project "$OPS_PROJECT" --buckets "$BUCKETS" \
  --images "postgres=${POSTGRES_IMAGE:-postgres:17-alpine},rushdb=${RUSHDB_IMAGE:-rushdb/rushdb:3.0.10},objectstore=${OBJECT_STORE_IMAGE:-rustfs/rustfs:1.0.5}" >&2

if [[ -n "${BACKUP_ENCRYPTION_KEY_FILE:-}" ]]; then
  ops_log "5/5 encrypt the payload (key id $(bk_key_id "$BACKUP_ENCRYPTION_KEY_FILE"))"
  bk_encrypt "$DEST" "$BACKUP_ENCRYPTION_KEY_FILE" || ops_die "backup encryption failed"
fi

if [[ "$KEEP" -gt 0 ]]; then
  # retention: delete older ukos-backup-* directories beyond the newest N (names sort chronologically)
  ls -1d "$OUT"/ukos-backup-* 2>/dev/null | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do
    ops_log "retention: removing $old"; rm -rf "$old"; done
fi
echo "$DEST"
