# Runbook: troubleshooting

Symptom -> cause -> fix. Details for the Windows host problems: `docs/development/local-runbook.md` section 7. Legend: **[V]** verified, **[U]** untested.

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | `os error 4551`, "DLL load failed ... Application Control policy" running pytest/ruff/mypy/psycopg on the host | Windows Application Control blocks unsigned shims and compiled DLLs | Call tools as `python -m <tool>`; run psycopg tests and mypy through `bash scripts/dev/linux-runner/run.sh <dir> "<cmd>"` [V] |
| 2 | Docker Desktop will not start: "listening on unix://...: The file cannot be accessed by the system" | stale AF_UNIX socket stubs after a forced kill | `powershell -ExecutionPolicy Bypass -File scripts\windows\fix-docker-sockets.ps1 -Start` [V] |
| 3 | `docker ps` returns "500 Internal Server Error ... dockerDesktopLinuxEngine" mid-run | Docker Desktop engine crashed (often under heavy parallel builds) | Wait 1-2 min; if still down quit and restart Docker Desktop; re-run. Do not run two heavy builds at once [V: seen during M08] |
| 4 | `bind: Only one usage of each socket address` / port already in use | another project uses a UKOS port | `docker ps --format "{{.Names}} {{.Ports}}"`; stop it or set `*_HOST_PORT` in `.env`. Windows may reserve ranges: `netsh interface ipv4 show excludedportrange protocol=tcp` [V] |
| 5 | `docker compose up --wait` exits 1 though all services look healthy | a finished one-shot with no dependent | keep the root `compose.override.yaml` (api depends on `objectstore-init`); for custom file sets see `tests/e2e/acceptance/compose.gate.yaml` [V] |
| 6 | Upload to the browser fails (CORS) or `objectstore-init` failed | bucket/CORS not created | `docker compose logs objectstore-init`, then `docker compose run --rm objectstore-init` (idempotent) [V] |
| 7 | Sign-in loops or "invalid redirect" | `OIDC_REDIRECT_URIS` / `WEB_PUBLIC_URL` / `OIDC_ISSUER_URL` differ from the URL in the browser | make all three use the same origin (127.0.0.1 vs localhost matter), `docker compose up -d dex web` [U] |
| 8 | Document stays `queued`/`processing` | worker or dispatcher down, or the embedding server unreachable | `docker compose ps`; `docker compose logs worker`; check LM Studio: `curl http://127.0.0.1:1234/v1/models` and that `google/embedding-gemma-300m` is listed; the job retries with backoff and resumes [V] |
| 9 | Document `failed`: embedding dimension/model mismatch | `AI_EMBEDDING_*` changed after ingesting | restore the original values (embedding space is fixed: BASELINE.md); a new model needs re-embedding [V] |
| 10 | Chat answers "cannot answer from your documents" | no evidence retrieved, or the model is down | check the document is `ready` and in a workspace you can read; `curl http://127.0.0.1:1234/v1/models` lists `google/gemma-3-4b`; LM Studio `lms server start` [V] |
| 11 | Chat or research fails with a timeout | model too slow / not loaded | `lms load google/gemma-3-4b --gpu max --context-length 4096 -y`; raise `CHAT_RUN_TIMEOUT_SECONDS` / `RESEARCH_TIMEOUT_SECONDS` [V] |
| 12 | Graph tab empty | `GRAPH_ENABLED=false` (default) or the document ingested before enabling | set `GRAPH_ENABLED=true`, `docker compose up -d api worker`, then `bash scripts/dev/graph-backfill.sh` [V] |
| 13 | Connector will not store a token | `CONNECTOR_ENCRYPTION_KEY` still a placeholder or not 32 bytes base64 | generate per install.md section 2 [V] |
| 14 | `docker compose build` hangs downloading packages | flaky container network to PyPI/npm | re-run; the build resumes from cache [V] |
| 15 | Postgres refuses the new password after editing `.env` | roles are created on first volume init only | change the role password inside Postgres, or wipe volumes on a new install (`docker compose down -v`: deletes data) [V] |
| 16 | Windows Defender Firewall prompts | Docker/Python/Node listening | deny (untick Private and Public): everything binds 127.0.0.1, loopback is not filtered [V] |
| 17 | RushDB data lost / will not open after changing mode | the node's multitenant mode is permanent per volume | never switch; restore from backup [V] |
| 18 | `scripts/ci/run_local.sh` web gate fails to pull `node:24-alpine` | no network to Docker Hub | retry; or run `npm ci && npm run lint && npm run typecheck && npm test && npm run build` in `apps/web` with host Node 24 [U] |
