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
