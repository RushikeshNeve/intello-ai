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
| NVIDIA driver (optional) | recent | Only for GPU-accelerated local inference. CPU works too. |

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
docker compose up -d --wait
docker compose wait objectstore-init      # one-shot bucket/CORS bootstrap; must exit 0
```

`up --wait` returns once long-running services are healthy, but **not** when the one-shot `objectstore-init` has
finished. That's why the second command is there.

### Services and ports (all bound to 127.0.0.1)

| Service | URL / port | Health |
|---|---|---|
| web (Next.js) | http://127.0.0.1:3300 | http://127.0.0.1:3300/api/health |
| api (FastAPI) | http://127.0.0.1:8300 (docs at `/docs`, schema `/openapi.json`) | `/health/live`, `/health/ready` |
| rushdb | http://127.0.0.1:3310 | container healthcheck (`rushdb health`) |
| objectstore (RustFS, S3 API) | http://127.0.0.1:9300 | container healthcheck |
| postgres | 127.0.0.1:5532 | `pg_isready` |
| redis | 127.0.0.1:6479 | `redis-cli ping` (password required) |
| ollama (optional, §4) | http://127.0.0.1:11434 | container healthcheck |

These ports form a dedicated **UKOS block** (DC-003). It avoids clashes with other local projects that use 3000/5432/6379/8000.

### Verify

```bash
docker compose ps
curl -fsS http://127.0.0.1:8300/health/ready     # {"status":"ok","checks":{"postgres":{"status":"ok"},"redis":{"status":"ok"}}}
curl -fsS http://127.0.0.1:3300/api/health       # {"status":"ok"}
```

## 4. Local AI (optional)

<!-- Filled in from M00.03.04 (docs/spikes/local-models.md) once the benchmark is merged. -->
See `docs/spikes/local-models.md` for the chosen models and the CPU fallback.

## 5. Stop and tear down

```bash
docker compose stop          # stop containers, keep everything
docker compose down          # remove containers + network, KEEP data volumes
docker compose down -v       # DESTROYS all local data (postgres, rushdb, objects). Only when you mean it.
```

On Windows, **quit Docker Desktop from the system tray**. Never kill it from Task Manager (see §7.3).

## 6. Development loops

### API (`apps/api`)

```bash
cd apps/api
uv venv --python <python.org python.exe> .venv   # Windows: see §7.1. Elsewhere: uv venv
uv sync --frozen
uv run --no-sync python -m ruff check
uv run --no-sync python -m ruff format --check
uv run --no-sync python -m pytest -q
```

Type checking (strict mypy). On Windows it runs in a Linux container (see §7.1):

```bash
docker run --rm -v "$(pwd):/src:ro" python:3.12-slim sh -c \
  'cp -r /src /work && cd /work && rm -rf .venv && pip install -q "uv>=0.11,<0.12" && uv sync -q --frozen && uv run --no-sync python -m mypy app tests'
```

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

### 7.2 Windows Defender Firewall prompts
Prompts for Docker, Python or Node can be **denied** (untick Private and Public, then Cancel). Every service binds to
127.0.0.1, and loopback traffic isn't filtered, so everything keeps working.

### 7.3 Docker Desktop crashes on start: "listening on unix://…: The file cannot be accessed by the system"
A force-killed Docker Desktop leaves stale AF_UNIX socket stubs that Windows can't delete (error 1920). Fix:
1. Quit Docker Desktop (tray → Quit).
2. Rename (don't delete) the folder named in the error, e.g.
   `%LOCALAPPDATA%\Docker\run` → `run.stale-<date>` or `%LOCALAPPDATA%\docker-secrets-engine` → `docker-secrets-engine.stale-<date>`.
3. Start Docker Desktop. It recreates the folder.

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
