# Operations toolkit (M08.02)

Scripts live in `scripts/ops/`, load tests in `tests/load/`, the optional observability stack in
`infrastructure/compose/observability.yml`. Everything runs in an isolated compose project and refuses the dev
project `ukos` unless `ALLOW_DEV_STACK=1` is set on purpose (a backup of the dev stack is legitimate; tests never do it).
Runbook-level instructions (install, incident handling) are in `docs/runbooks/` (agent C); this file is the reference
for the tools themselves.

## Backup (`scripts/ops/backup.sh`, `backup.ps1`)

```bash
OPS_PROJECT=ukos ALLOW_DEV_STACK=1 bash scripts/ops/backup.sh --out D:/backups --keep 7    # Git Bash
.\scripts\ops\backup.ps1 -Project ukos -Out D:\backups -Keep 7                              # PowerShell wrapper
```

One directory `ukos-backup-<UTC timestamp>/`:

| File | Content |
|---|---|
| `postgres.dump` | `pg_dump -Fc --no-owner` of the whole database (all schemas, RLS policies, functions, grants). Roles are not in the dump: the init scripts and migrations of a fresh stack create them. |
| `rushdb-data.tgz` | **Cold snapshot** of the RushDB volume (DC-009 condition 4): RushDB is stopped, the volume is tarred, RushDB is started again (search is unavailable for the copy time, seconds for the local data sizes). |
| `objects/<bucket>/...` | S3-level export (`aws s3 sync`) of the `originals` and `artifacts` buckets, online. |
| `rowcounts.txt`, `rushdb-counts.json` | exact row count per table; per tenant: chunk counts, index names and states, and a BM25 probe (term + hit count). |
| `manifest.json`, `SHA256SUMS` | versions (**migration head, embedding space ids, pipeline versions**, image tags), counts, sha256 and size of every file and object. `contains_env_file` is always `false`. |

Never included: `.env` or any credential, Redis (broker only, ADR-036), Dex state. Keep backups where the `.env`
secrets are not stored together with them. `--keep N` deletes all but the newest N backups under `--out`.

Consistency: the dump, the object export and the RushDB snapshot are taken one after the other, not atomically.
Stop the `worker` and `dispatcher` first for a quiesced backup (`docker compose stop worker dispatcher`); otherwise a
document finishing in between is repaired after the restore by the reconcile sweep (a RushDB chunk without a
PostgreSQL revision is never visible: search revalidates every hit against PostgreSQL).
Compatibility: restore with the same release (migration head) that wrote the backup. The restore script refuses a
mismatch and tells you which head the backup has.

## Restore (`scripts/ops/restore.sh`) and the rehearsal

```bash
OPS_PROJECT=ukos-restore bash scripts/ops/restore.sh D:/backups/ukos-backup-20261120T101500Z --start-app
```

Restores into a **fresh** compose project (refuses one that already has volumes), then verifies automatically:
manifest checksums (also: no `.env`), migration head, exact row counts of every table, object checksums (re-export of
the restored buckets), RushDB chunk counts per tenant, text index `chunk-text` and the vector index `ready`, BM25 probe
hits identical to the backup (RushDB 3.0.10 condition: a physical copy can come up with an empty BM25; the probe
detects it, and the fix is dropping and re-creating the text index). Exit code 1 on any mismatch.

`bash scripts/ops/rehearsal.sh` runs the whole drill in two isolated projects: seed (two tenants, documents through
the real API) -> backup -> destroy the source project including volumes -> restore into a fresh project -> start the app ->
`scripts/ops/rehearsal.mjs verify`: sign in again, list, **search finds the seeded document at rank 1, its citation
(chunk evidence) resolves, a graph query answers, and the other user still gets a concealed 404**. It replaces the
M00 `scripts/restore_smoke.sh` (kept for its data-store-only fixtures). Latest numbers: see "Verified runs" below.

## Load and fault injection (`tests/load/`)

`bash tests/load/run.sh load` (or `trace`, `all`; `PHASES=upload,search` for a subset) boots the full stack in
`ukos-m08-b-load`, runs the phases of `tests/load/load.mjs` and writes `docs/ops/load-results.md` with the machine
description. Phases: upload burst + queue drain, concurrent searches, chat runs through `chat-stub.mjs`, and the faults
Redis restart, PostgreSQL restart, embedder down (the M03 gate already covers worker/dispatcher kill, RushDB stop and
lease expiry; they are not repeated). The only hard check is integrity after every phase.

## Tracing, logs, metrics (M08.02.04)

* **Correlation:** the API middleware reads `traceparent` and `X-Request-Id` (or mints them), answers with both, and every
  log line of the request carries `trace_id`, `request_id` and `org` (a 12-character hash, never the id). Outbox rows
  written during the request store `payload.trace` (traceparent of the request span + request id); the dispatcher puts
  them into the Celery message headers; the worker binds them around the task (`job_id` = the first task argument);
  the httpx clients of the embedding and chat adapters send child `traceparent` and `X-Request-Id` headers.
  Rows without a context (older rows, reaper-created rows) get a fresh trace in the worker.
* **Logs:** one JSON object per line (`UKOS_LOG_FORMAT=json`, default; `text` restores the old format). Fields:
  `ts, level, logger, service, msg, trace_id, request_id, org, job_id` and an allow-list of `task, stage, error_code,
  duration_ms, status, method, route`. Exceptions log only their type. Request lines use the route **template**, never
  the path or query. Uvicorn's access log is replaced by this request line.
* **OpenTelemetry:** `OTEL_ENABLED=false` by default. When true, the SDK exports spans (HTTP request, task) over OTLP/HTTP to
  `OTEL_EXPORTER_OTLP_ENDPOINT`. Span attributes are names and counts only.
* **Metrics:** `METRICS_PORT=0` (off) by default. When set, a separate listener serves Prometheus metrics for the process:
  API `ukos_http_request_duration_seconds{method,route,status}`; worker `ukos_task_duration_seconds{task,outcome}`
  (`ingest.run_job` = ingestion, `chat.run`, `research.run`); dispatcher `ukos_outbox_dispatched_total{task}` and
  `ukos_queue_depth{queue}` (Redis list length). Labels are route templates, task and queue names: no ids, no content.
  The listener is not routed by the API and the compose files `expose` it on the compose network only.
* **Profile `observability`:** `docker compose -f compose.yaml -f compose.override.yaml -f infrastructure/compose/observability.yml --profile observability up -d`
  turns the above on for api/worker/dispatcher and adds the OpenTelemetry collector, Jaeger (16686), Prometheus (9390)
  and Grafana (3320, user `admin`, password `GRAFANA_ADMIN_PASSWORD`, default `change-me-grafana`: change it) with the
  provisioned dashboard "Intelo AI - overview" (request latency overall and per route, request/error rate, queue depth,
  ingestion/chat/research run durations, task error rate, outbox throughput). Without the overlay nothing changes.
* **Proof:** `apps/api/tests/observability/test_tracing.py` (unit: header, outbox payload, Celery headers, worker context,
  no content in logs) and `tests/load/trace.mjs` (integration: one trace id appears in the API, dispatcher and worker logs
  of an ingestion job; the request id is identical; no content, org id or token in any log).

## Verified runs

See the end of this file once the runs are recorded (the numbers are written by the scripts, not typed).
