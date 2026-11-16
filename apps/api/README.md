# ukos-api

FastAPI starter for Universal Knowledge OS (M00.02.05). See `docs/plan/` for scope.

```sh
uv sync
uv run ruff check && uv run ruff format --check && uv run mypy app && uv run pytest
uv run uvicorn app.main:create_app --factory --reload --port 8000
```
