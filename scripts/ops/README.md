# scripts/ops: backup, restore, rehearsal

Full reference (contents of a backup, consistency, tracing/metrics, load tests): `docs/ops/README.md`.
Runbook for people: `docs/runbooks/backup-restore.md`.

| Script | What it does |
|---|---|
| `backup.sh` / `backup.ps1` | One backup directory: `pg_dump -Fc`, cold RushDB volume snapshot, S3-level export of both buckets, row counts, `manifest.json` + `SHA256SUMS`. Never contains `.env`, Redis or Dex state. |
| `restore.sh <dir>` | Restores into a fresh compose project and verifies checksums, migration head, exact row counts, object checksums, RushDB chunk counts per tenant, indexes and the BM25 probe. Exit 1 on any mismatch. |
| `rehearsal.sh` | Full drill in isolated projects: seed, backup, destroy, restore, start app, sign in, search, citation, graph query, tenant isolation. |
| `stack.sh`, `lib.sh` | Shared helpers (isolated full stack on the M08 agent B port block). Refuse project `ukos` unless `ALLOW_DEV_STACK=1`. |

```bash
OPS_PROJECT=ukos ALLOW_DEV_STACK=1 bash scripts/ops/backup.sh --out D:/backups --keep 7
OPS_PROJECT=ukos-restore bash scripts/ops/restore.sh D:/backups/ukos-backup-<UTC> --start-app
bash scripts/ops/rehearsal.sh
```

Schedule: daily backup (Windows Task Scheduler running `backup.ps1`, or cron running `backup.sh`); `--keep 7` is the
default retention (newest 7 kept). Rehearse a restore after every release and at least monthly. Restore only with the
release (migration head) that wrote the backup.
