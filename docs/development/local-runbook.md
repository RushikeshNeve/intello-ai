# Local Developer Runbook

Task M00.01.03 · Applies to: Windows 11 (primary dev machine), macOS and Linux (same commands minus the Windows notes).
Everything runs locally with **$0 recurring cost**. No cloud account is required.

## 1. Prerequisites (clean machine)

| Tool | Version used | Notes |
|---|---|---|
| Docker Desktop (WSL 2 backend on Windows) | 29.3.x, Compose v5 | Give the VM ≥ 8 GB RAM (12 GB recommended with local AI). |
| Git | 2.5x | Line endings are pinned to LF by `.gitattributes`. |
| Python | 3.12, **python.org installer** on Windows | See §7.1 for why not uv-managed Python. |
| uv | 0.11.x | `pip install uv` or the official installer. |
| Node.js + npm | Node 24, npm 11 | npm is the package manager (no pnpm). |
| LM Studio (optional, for local AI) | 0.4.9, `lms` CLI | Runs natively on the host and serves the OpenAI-compatible API on 127.0.0.1:1234. See §4. |
| NVIDIA driver (optional) | recent | GPU acceleration for LM Studio. CPU works too, just slower. |

## 2. First-time setup

```bash
git clone <repo-url> universal-knowledge-os
cd universal-knowledge-os
cp infrastructure/env/.env.example .env
```

Edit `.env` and replace **every** `change-me-*` value. Generate secrets with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Keep `DATABASE_URL`, `DATABASE_MIGRATION_URL`, `REDIS_URL` and `CELERY_BROKER_URL` consistent with the passwords you set.
`.env` is git-ignored. Never commit it.

> **Important:** the PostgreSQL init script creates the app role only when the volume is first created, and RushDB
> fixes its multitenant mode at that point too. If you change `.env` passwords after the first start, you must
> either wipe the volumes (§5) or change the role password by hand.

## 3. Start the stack

```bash
docker compose up -d --wait        # exits 0 when every service is healthy and both one-shots succeeded
```

Two one-shot jobs run during start-up: `api-migrate` (database migrations) and `objectstore-init` (creates the
buckets and CORS). The `api` waits for both. The root `compose.override.yaml`, which Docker loads automatically,
makes the API depend on `objectstore-init`; without a dependent, Compose treats a finished one-shot as a failed
`--wait` (exit code 1, even though everything is healthy). A cold start from nothing takes about 30 s once the images
are built. To re-run the bucket setup by hand: `docker compose run --rm objectstore-init` (idempotent).

For hands-on testing in the browser, see [manual-test-guide.md](manual-test-guide.md).

### Services and ports (all bound to 127.0.0.1)

| Service | URL / port | Health |
|---|---|---|
| web (Next.js) | http://127.0.0.1:3300 | http://127.0.0.1:3300/api/health |
| api (FastAPI) | http://127.0.0.1:8300 (docs at `/docs`, schema `/openapi.json`) | `/health/live`, `/health/ready` |
| rushdb | http://127.0.0.1:3310 | container healthcheck (`rushdb health`) |
| objectstore (RustFS, S3 API) | http://127.0.0.1:9300 | container healthcheck |
| postgres | 127.0.0.1:5532 | `pg_isready` |
| redis | 127.0.0.1:6479 | `redis-cli ping` (password required) |
| LM Studio (host, optional, §4) | http://127.0.0.1:1234/v1 (containers: `host.docker.internal:1234`) | `lms server status` |

These ports form a dedicated **UKOS block** (DC-003). It avoids clashes with other local projects that use 3000/5432/6379/8000.

### Verify

```bash
docker compose ps
curl -fsS http://127.0.0.1:8300/health/ready     # {"status":"ok","checks":{"postgres":{"status":"ok"},"redis":{"status":"ok"}}}
curl -fsS http://127.0.0.1:3300/api/health       # {"status":"ok"}
```

## 4. Local AI (optional): LM Studio

Local inference runs in **LM Studio on the host**, not in a container (DC-011). It uses the GPU directly. The
platform talks to it only through the OpenAI-compatible `/v1` API, so any other `/v1` server can replace it.

```bash
lms get google/embedding-gemma-300m        # default embedding model (768 dimensions, about 229 MB)
lms get google/gemma-3-4b                  # default chat model (about 3.3 GB)
lms load google/embedding-gemma-300m --gpu max -y
lms load google/gemma-3-4b --gpu max --context-length 4096 -y
lms server start                           # serves http://127.0.0.1:1234
curl -fsS http://127.0.0.1:1234/v1/models  # both models listed
```

On Windows, if the `lms` command isn't found, use `%USERPROFILE%\.lmstudio\bin\lms.exe`.

| Machine | What to run | Measured (M00.03.04, RTX 5050 8 GB) |
|---|---|---|
| NVIDIA GPU, ≥ 6 GB VRAM | both defaults with `--gpu max` | chat 57 tok/s; about 0.9 s per grounded answer; about 4.9 GB VRAM for both models |
| CPU only | the same two models (no re-embedding needed) | chat about 12 tok/s; about 3.8 s per grounded answer |
| Low RAM (< 8 GB free) | embeddings only (embedding-gemma uses about 220 MB); generation off or sent to an allowed provider | — |

- Don't load models larger than your free VRAM plus RAM. `gpt-oss-20b` nearly exhausted 24 GB of RAM on the test machine.
- LM Studio loads any downloaded model on first request ("just-in-time"). The gateway must only request
  allow-listed models from `AI_CHAT_MODEL` and `AI_EMBEDDING_MODEL`.
- **Licence:** LM Studio is free for personal and internal use only, and closed-source. Hosted or SaaS deployments must use
  llama.cpp `llama-server`, Ollama or vLLM behind the same `AI_BASE_URL`.
- Re-run the benchmark after changing models:
  `uv run --no-project --with httpx python -I scripts/benchmarks/local_models.py --label gpu --gpu max --embed-models google/embedding-gemma-300m --chat-models google/gemma-3-4b --restore google/gemma-3-4b`
  (details and every result: `docs/spikes/local-models.md`).

## 5. Stop and tear down

```bash
docker compose stop          # stop containers, keep everything
docker compose down          # remove containers + network, KEEP data volumes
docker compose down -v       # DESTROYS all local data (postgres, rushdb, objects). Only when you mean it.
```

On Windows, **quit Docker Desktop from the system tray**. Never kill it from Task Manager (see §7.3).

## 6. Development loops

### API (`apps/api`)

On Linux/macOS, run Python directly:

```bash
cd apps/api && uv sync --frozen
uv run python -m ruff check && uv run python -m ruff format --check
uv run python -m pytest -q
uv run python -m mypy app tests migrations
```

On this Windows machine, anything that imports psycopg (the unit suite, every database test) or mypy must run
in Linux, because Application Control blocks their compiled files on the host (§7.1, DC-018). Use the repo's
runner. It builds a small image once, mounts the repo, and passes `UKOS_*`, `POSTGRES_*`, `REDIS_*`, `API_*`,
`WEB_*` and `OIDC_*` variables through:

```bash
scripts/dev/linux-runner/run.sh apps/api "python -m pytest -q tests"
scripts/dev/linux-runner/run.sh apps/api "python -m mypy app tests migrations"
UKOS_TEST_COMPOSE_PROJECT=ukos-v-x POSTGRES_HOST_PORT=35801 REDIS_HOST_PORT=36801 \
  scripts/dev/linux-runner/run.sh apps/api "python -m pytest -q ../../tests/integration/m01_tenancy"
scripts/dev/linux-runner/run.sh . "python -m pytest -q tests/security/postgres_rls"
```

Ruff has no compiled-extension problem on the host (`python -m ruff` from any venv works).

### Web (`apps/web`)

```bash
cd apps/web
npm ci
npm run lint
npm run typecheck     # runs `next typegen` first, so it works on a fresh clone
npm test
npm run build
```

### Infrastructure and spike tests (from the repo root)

Each suite starts its **own** compose project and cleans up after itself. Don't run two suites that use the same host
ports at the same time.

```bash
uv run --no-project --with pytest python -m pytest -q tests/docs
uv run --no-project --with pytest --with "psycopg[binary]" --with redis python -m pytest -q tests/infrastructure/test_data_services.py
uv run --no-project --with pytest --with "psycopg[binary]" --with redis python -m pytest -q tests/infrastructure/test_apps_health.py
docker compose -p ukos-rushdb --env-file infrastructure/env/.env.example -f infrastructure/compose/rushdb.yml up -d --wait
uv run --no-project --with pytest --with httpx python -m pytest -q tests/spikes/rushdb_persistence.py tests/spikes/rushdb_tenants.py tests/spikes/rushdb_hybrid.py tests/spikes/rushdb_graph.py
docker compose -p ukos-rushdb --env-file infrastructure/env/.env.example -f infrastructure/compose/rushdb.yml down -v
uv run --no-project --with pytest --with boto3 --with httpx python -m pytest -q tests/spikes/objectstore_integration.py
bash scripts/restore_smoke.sh
```

Set `UKOS_TEST_COMPOSE_PROJECT=<name>` to run the infrastructure tests under a different project name.

## 7. Troubleshooting

### 7.1 Windows Application Control ("os error 4551", "DLL load failed … Application Control policy")
This machine enforces an Application Control policy (WDAC / Smart App Control). It blocks some unsigned executables and DLLs:
- **`pytest.exe`, `ruff.exe` and other uv-generated launcher shims.** Always invoke tools as `python -m <tool>`.
- **uv-managed CPython.** Its `python.exe` is blocked, so build venvs from the python.org install
  (`uv venv --python C:/Users/<you>/AppData/Local/Programs/Python/Python312/python.exe .venv`).
- **mypy's compiled (mypyc) modules.** Run mypy in a Linux container (§6).
- **Compiled extension DLLs: psycopg's binary driver (`DLL load failed while importing pq`) and mypyc.** On 2026-11-16 these loaded from a venv under `%LOCALAPPDATA%`. **By 2026-11-18 they were blocked everywhere on the host, including fresh isolated `uv run --no-project --with "psycopg[binary]"` environments.** The policy can change between days, so don't rely on any host workaround. Run every test that imports psycopg, and mypy, through `scripts/dev/linux-runner/run.sh` (§6). Tests that need only Docker and HTTP still run on the host: the Node suites in `tests/e2e/*`, `tests/docs`, and `tests/infrastructure/test_env_contract.py`. `test_data_services.py` and `test_apps_health.py` import psycopg, so run them through the runner too.

### 7.2 Windows Defender Firewall prompts
Prompts for Docker, Python or Node can be **denied** (untick Private and Public, then Cancel). Every service binds to
127.0.0.1, and loopback traffic isn't filtered, so everything keeps working.

### 7.3 Docker Desktop crashes on start: "listening on unix://…: The file cannot be accessed by the system"
A force-killed Docker Desktop leaves stale AF_UNIX socket stubs that Windows can't delete (error 1920). Fix:
1. Quit Docker Desktop (tray → Quit).
2. Rename (don't delete) the folder named in the error, e.g.
   `%LOCALAPPDATA%\Docker\run` → `run.stale-<date>` or `%LOCALAPPDATA%\docker-secrets-engine` → `docker-secrets-engine.stale-<date>`.
3. Start Docker Desktop. It recreates the folder.

Shortcut: `powershell -ExecutionPolicy Bypass -File scripts\windows\fix-docker-sockets.ps1 -Start` does steps 2–3 safely. It refuses while Docker is running and only renames folders that contain nothing but socket stubs.

### 7.4 Port already in use
Another local project may publish one of the UKOS ports. Find it with `docker ps --format "{{.Names}} {{.Ports}}"`
(or `netstat -ano | findstr :<port>`) and stop that project, or override the `*_HOST_PORT` values in `.env`.

### 7.5 `objectstore-init` failed or CORS missing
`docker compose logs objectstore-init`. Re-running is safe: `docker compose run --rm objectstore-init`.

### 7.6 Docker builds time out downloading packages
Container access to PyPI or npm can be flaky on this network. Re-run `docker compose build`. It resumes from cache.

## 8. Backup and restore (developer smoke)

`bash scripts/restore_smoke.sh` proves backup and restore across PostgreSQL, RushDB and the object store in an
isolated project (`ukos-restore-smoke`). It never touches your `ukos` stack. Production-grade automated backups are
M08.02.01.
