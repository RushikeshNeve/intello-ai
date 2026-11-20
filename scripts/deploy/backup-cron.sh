#!/usr/bin/env bash
# Scheduled backup of the production stack: runs scripts/ops/backup.sh (pg_dump + cold RushDB snapshot + S3 export +
# manifest), keeps the newest BACKUP_KEEP, optionally copies the new backup off the VM with rclone. UNTESTED on a VM.
#   bash scripts/deploy/backup-cron.sh                 # one backup now
#   bash scripts/deploy/backup-cron.sh --install-cron  # print the crontab line (daily 03:15); does not edit crontab
# Config (from .env.prod): BACKUP_DIR (default /var/backups/ukos), BACKUP_KEEP (7), BACKUP_RCLONE_REMOTE (empty = local only).
# Backups contain user documents and the database: protect the directory (chmod 700) and encrypt the remote.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_env_file
if [[ "${1:-}" == "--install-cron" ]]; then
  echo "15 3 * * * cd $DEPLOY_ROOT && bash scripts/deploy/backup-cron.sh >> $STATE_DIR/backup.log 2>&1"
  echo "# add with: crontab -e   (the RushDB cold snapshot stops search for a few seconds each run)"
  exit 0
fi
BACKUP_DIR="$(env_get BACKUP_DIR)"; BACKUP_DIR="${BACKUP_DIR:-/var/backups/ukos}"
KEEP="$(env_get BACKUP_KEEP)"; KEEP="${KEEP:-7}"
REMOTE="$(env_get BACKUP_RCLONE_REMOTE)"
mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"

# backup.sh works on the ops compose subset; point it at the whole prod file set so containers are not
# recreated with dev settings (OPS_COMPOSE_FILES hook added to scripts/ops/lib.sh).
export OPS_PROJECT="$PROJECT" ENV_FILE="$ENV_FILE"
export OPS_COMPOSE_FILES="${COMPOSE_FILES[*]}"
export UKOS_IMAGE_TAG="$(cat "$STATE_DIR/current_tag" 2>/dev/null || echo local)"
DEST="$(bash "$DEPLOY_ROOT/scripts/ops/backup.sh" --out "$BACKUP_DIR" --keep "$KEEP" | tail -1)"
log "backup written: $DEST"

if [[ -n "$REMOTE" ]]; then
  command -v rclone >/dev/null || die "BACKUP_RCLONE_REMOTE is set but rclone is not installed"
  log "off-VM copy to $REMOTE"
  rclone copy "$DEST" "$REMOTE/$(basename "$DEST")" --transfers 4 --checksum
  rclone check "$DEST" "$REMOTE/$(basename "$DEST")" --one-way >&2 || die "off-VM copy verification failed"
  log "off-VM copy verified (remote retention is the remote's own lifecycle policy; prune old ones there)"
else
  log "BACKUP_RCLONE_REMOTE empty: local copy only. A single-VM backup does not survive losing the VM."
fi
