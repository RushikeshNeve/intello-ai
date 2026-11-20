# Runbook: day-to-day operations

Legend: **[V]** verified in the project, **[T]** run while writing this, **[U]** UNTESTED. Run from the repo root in Git Bash
(`export MSYS_NO_PATHCONV=1`). Your stack is the compose project `ukos`.

## Health and status
```bash
docker compose ps                                     # [V] every service healthy; api-migrate/objectstore-init exited 0
curl -fsS http://127.0.0.1:8300/health/live           # [V]
curl -fsS http://127.0.0.1:8300/health/ready          # [V] postgres + redis checks
curl -fsS http://127.0.0.1:3300/api/health            # [V]
curl -fsS http://127.0.0.1:1234/v1/models             # [V] LM Studio (host)
docker compose logs --no-color --tail 100 api worker dispatcher   # [V] logs never contain tokens or document text (checked by the M03 gate)
```
Per-document state: the document page in the web app, or `GET /api/v1/orgs/{org}/workspaces/{ws}/documents/{doc}/ingestion`.

## Start, stop, restart, upgrade
```bash
docker compose up -d --wait             # [V]
docker compose restart api worker       # [V] safe: ingestion jobs hold leases and resume; chat/research runs are fenced
docker compose stop                     # [V] data stays in volumes
git pull && docker compose up -d --build --wait    # [V] upgrade: api-migrate runs Alembic to the new head first
```
Before an upgrade take a backup (`backup-restore.md`) and read the release notes in `docs/release/BASELINE.md`.
Migrations are forward-only; to roll back restore the backup taken before the upgrade.

## Queues and workers
- Ingestion: outbox rows -> `dispatcher` -> Redis (Celery broker) -> `worker`. Redis is NOT durable truth: if it is lost,
  the reconcile sweeper (`INGEST_RECONCILE_INTERVAL_SECONDS`, default 300 s) re-queues work from PostgreSQL. [V: M03 crash-injection gate]
- Stuck document: wait one reconcile interval; otherwise use "Retry" in the document UI.
  Jobs end `dead` after `INGEST_MAX_ATTEMPTS` (default 5); the failure code is on the ingestion status.
- Graph build runs only when `GRAPH_ENABLED=true`; research and memory invalidation run in the worker.

## Capacity and limits
Per-org storage quota `ORG_STORAGE_QUOTA_BYTES` (5 GiB), max upload `UPLOAD_MAX_BYTES` (100 MiB), chat rate limits
`CHAT_MAX_RUNS_PER_MIN` / `CHAT_MAX_CONCURRENT_RUNS`, research limits `RESEARCH_*`. Changing any of them: edit `.env`, `docker compose up -d`.
Disk: `docker system df`; volumes: `docker volume ls --filter name=ukos` [V].

## Retention and deletion
Deleting a document denies access immediately and purges objects/index after `DOCUMENT_PURGE_GRACE_DAYS` (default 7).
Memories backed by a deleted document or a revoked source are invalidated automatically (verified by the acceptance suite).

## Secrets rotation
| Secret | Procedure |
|---|---|
| `CONNECTOR_ENCRYPTION_KEY` | generate a new key (install.md), set `CONNECTOR_ENCRYPTION_KEY_ID=k2`, put the old one in `CONNECTOR_ENCRYPTION_OLD_KEYS=k1:<old base64>`, `docker compose up -d api worker` [U] |
| `OIDC_CLIENT_SECRET`, `WEB_SESSION_SECRET` | change in `.env`, `docker compose up -d dex web api`; every user must sign in again [U] |
| Postgres / Redis / RushDB / S3 secrets | fixed into volumes at first start: change the value inside the service first, then `.env`, then restart (see security-incident.md section 1) [U] |

## Tests and gates
`bash scripts/ci/run_local.sh` (CI gates), `bash tests/e2e/acceptance/run.sh` (full-stack acceptance), see `docs/release/CHECKLIST.md`.
