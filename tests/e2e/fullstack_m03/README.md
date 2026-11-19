# M03 gate: full-stack ingestion, retry, dead-letter and crash-injection tests

Runs the real stack (PostgreSQL, Redis, Dex, RustFS + init, RushDB, `api-migrate`, `api`, `web`, and the
`worker` and `dispatcher` containers built from the repo images) in the isolated compose project
`ukos-e2e-m03`, and drives it over HTTP like a browser (`fullstack.test.mjs`, Node's built-in test runner).
It also covers task M03.03.05 (retry, dead-letter and crash-injection: "crash at each stage resumes
without duplicates or partial exposure").

```bash
bash tests/e2e/fullstack_m03/run.sh                 # build, run everything (about 10 minutes), tear down
KEEP=1 bash tests/e2e/fullstack_m03/run.sh          # leave the stack up (web http://127.0.0.1:33600)
GATE_ONLY='^(0|1|2)\.' bash tests/e2e/fullstack_m03/run.sh   # node --test name pattern (0 = setup is always needed)
```

Ports (outside every other suite): web 33600, api 38600, dex 38680, postgres 35900, redis 36900,
object store 39600, RushDB 33610, embedding stub 18334. The `ukos` dev stack is never touched.

## How it is wired

* `run.sh` starts the stack with the same compose files as the dev stack plus `compose.gate.yaml` (api and
  worker wait for the bucket init; worker waits for RushDB) and shortens the timers through the variables the
  compose files already expose: `INGEST_LEASE_SECONDS=20`, `INGEST_HEARTBEAT_SECONDS=5`,
  `INGEST_REAP_INTERVAL_SECONDS=5`, `INGEST_RETRY_BASE_SECONDS=2`, `INGEST_MAX_ATTEMPTS=4`,
  `INGEST_RECONCILE_INTERVAL_SECONDS=15`, concurrency 2.
* The embedding server is `tests/e2e/support/embedding-stub.mjs` on the host (dimensions 768, model
  `google/embedding-gemma-300m`), started and stopped **by the test file** (scenario 6d needs "connection
  refused"); containers reach it as `http://host.docker.internal:18334/v1`. `run.sh` releases the port in its
  exit trap as a safety net.
* Under-the-hood checks: `docker exec ... psql` as admin (and as `ukos_worker`/`ukos_app` for the security
  checks), the RushDB HTTP protocol directly with the operator token (same wire format as the adapter), and an
  `amazon/aws-cli` container for the artifacts bucket.
* At the end `run.sh` scans the api, web, worker and dispatcher logs for tokens, secrets, presigned-URL
  material and the document-text canary (`GATE-CANARY`); the test scans every API and SSE response for
  internal fields.

## Scenarios

| # | What it proves |
|---|---|
| 0 | stack healthy, `ukos_worker` role created by the migration |
| 1 | TXT, MD, HTML and a generated PDF: queued -> processing -> ready; revision `published`, pointer, chunks in RushDB (`published=true`), artifacts, audit event, event timeline, SSE, no internals |
| 2 | search contract at RushDB level: published-only (keyword and vector), workspace predicate, org B tenant sees nothing |
| 3 | new version: v1 stays published until v2 publishes; sampled invariants (never v2 visible before the pointer moves, never v1 hidden while it is the pointer); v1 chunks, artifacts purged |
| 4 | idempotency: duplicate finalize, duplicate/re-sent outbox rows (while running and after success), manual re-queue of a finished job |
| 5 | unsupported (png, xlsx), no text (empty PDF), corrupt PDF; dead-letter after exhausted attempts, `can_retry`, viewer 403, concurrent retry single winner, fix and retry to `ready` |
| 6a-e | crash injection: kill worker mid-embed (lease, reaper, resume skips finished stages), kill dispatcher before dispatch, stop RushDB mid-index, stop the embedding server, reconcile sweep repairing induced drift |
| 7 | deletion: immediate 404, revisions purged, running job cancelled, chunks and artifacts removed, vector-index statistics reported |
| 8 | outsiders get concealed 404 (status, retry, SSE), viewer cannot retry, no host ports on worker/dispatcher, no RushDB token in web/api, `ukos_worker` is RLS-bound |
| 9 | 20 small documents at once: all ready; total, p50, p95 reported |
| 99 | no internal fields or document text in any response; the `GATE-REPORT` line carries the numbers |
