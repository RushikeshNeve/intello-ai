# ukos-api

FastAPI starter for Universal Knowledge OS (M00.02.05). See `docs/plan/` for scope.

```sh
uv sync
uv run ruff check && uv run ruff format --check && uv run mypy app && uv run pytest
uv run uvicorn app.main:create_app --factory --reload --port 8000
```

On the Windows dev host, keep the venv under `%LOCALAPPDATA%` (Application Control blocks compiled
DLLs such as psycopg's driver when the venv is on D:) and call tools as `python -m <tool>`:

```sh
export UV_PROJECT_ENVIRONMENT="C:/Users/<you>/AppData/Local/ukos/venvs/api"
uv venv --python C:/Users/<you>/AppData/Local/Programs/Python/Python312/python.exe "$UV_PROJECT_ENVIRONMENT"
uv sync --frozen && uv run --no-sync python -m pytest -q
```

## Database (M01.02.01)

* `app/core/models.py`: ORM models; `app/core/db.py`: engine and `tenant_session(user_id=, org_id=)`.
* Migrations run as the **admin** role (`DATABASE_MIGRATION_URL`), never as the app role. Grants go
  to `POSTGRES_APP_USER` (or `-x app_role=`, default `ukos_app`). In compose, the one-shot
  `api-migrate` service runs `python -m alembic upgrade head` before `api` starts.

```sh
DATABASE_MIGRATION_URL=postgresql+psycopg://ukos_admin:<pw>@127.0.0.1:5532/ukos \
  uv run --no-sync python -m alembic upgrade head
```

DB-backed tests: `tests/integration/m01_foundation/` (see its `conftest.py` for port overrides).
