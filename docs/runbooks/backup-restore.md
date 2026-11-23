# Runbook: backup and restore

**Automated backups, restore rehearsal and their scripts are delivered by M08.02 and live in `scripts/ops/`: see
`scripts/ops/README.md` for the commands, schedule and retention. This page does not duplicate them.**
(If `scripts/ops/` is not in your checkout yet, that work has not been merged; use the developer smoke below.)

## What must be backed up (four stores + one file)
| Data | Where | Why it matters |
|---|---|---|
| PostgreSQL | volume of service `postgres` | authoritative: orgs, users, grants, documents, chat, memory, research, audit |
| Object store | volume of service `objectstore` (buckets `ukos-originals`, `ukos-artifacts`) | original files and derived artifacts |
| RushDB | volume of service `rushdb` | knowledge index (vectors, keywords, graph); can be rebuilt from PostgreSQL + objects by re-ingesting, but slowly |
| Redis | volume of service `redis` | NOT truth (Celery broker only): never needs a backup |
| `.env` | repo root | passwords and keys; store it in your password manager, NOT with the data backups. Without `CONNECTOR_ENCRYPTION_KEY` stored connector tokens are unreadable |

Restore order: `.env` -> PostgreSQL -> object store -> RushDB -> `docker compose up -d --wait` -> check (`operations.md` health section).
Take the backup of the three stores at the same moment (stop `api worker dispatcher` for a consistent copy: `docker compose stop api worker dispatcher web`).

## Developer smoke (exists today)
```bash
bash scripts/restore_smoke.sh      # [V] seeds an isolated project `ukos-restore-smoke`, backs up all stores with a SHA-256
                                   # manifest, destroys the project incl. volumes, restores, compares digests. Never touches `ukos`.
KEEP=1 bash scripts/restore_smoke.sh   # keep the restored stack for inspection
```
It proves the procedure and the digests; it is not a scheduled backup.

## Rules
- A backup you have not restored is not a backup: rehearse the restore on a separate project/ports (never over `ukos`) after any change to versions or volumes.
- Keep at least two generations, one off the machine (external disk or cloud storage you control). Encrypt off-machine copies; they hold document text.
- Deleted documents remain in old backups until those expire (security-incident.md section 4).
- Pin versions: restore with the same image tags as the backup (`docs/release/BASELINE.md`); Alembic then upgrades the schema on start.

## Master key (M14.08 envelope encryption, optional)
Off by default: with `MASTER_KEY_FILE` empty, connector tokens use the single `CONNECTOR_ENCRYPTION_KEY` as before.
To turn it on, keep the key file OUTSIDE the repo and the data backups, and mount it read-only into `api`, `worker` and `dispatcher` with a local override file (never commit the key or the override):
```yaml
# compose.override.yml (or a prod override next to .env.prod)
services:
  api:        { volumes: ["/etc/ukos/master.key:/run/secrets/ukos_master_key:ro"] }
  worker:     { volumes: ["/etc/ukos/master.key:/run/secrets/ukos_master_key:ro"] }
  dispatcher: { volumes: ["/etc/ukos/master.key:/run/secrets/ukos_master_key:ro"] }
```
Then set `MASTER_KEY_FILE=/run/secrets/ukos_master_key` and `MASTER_KEY_ID` in `.env` / `.env.prod` (`apps.yml` and `workers.yml` pass `KEY_PROVIDER`, `MASTER_KEY_FILE`, `MASTER_KEY_ID`, `MASTER_OLD_KEYS` through). Create the file with `openssl rand -base64 32 > master.key; chmod 400 master.key`.
- Back the key file up like `.env` (password manager), never with the data: losing it makes every per-org data key, and so every sealed connector token, unreadable.
- Rotation and crypto-shred: `scripts/ops/rotate-keys.sh` (security-incident.md section 1); encrypted backups: `BACKUP_ENCRYPTION_KEY_FILE` (`scripts/ops/README.md`).
