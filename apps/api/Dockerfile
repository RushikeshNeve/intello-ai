# syntax=docker/dockerfile:1
# Universal Knowledge OS API (M00.02.05). Build context: apps/api

FROM python:3.12-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
# uv from PyPI (official package), pinned to the minor line used to produce uv.lock.
RUN pip install "uv>=0.11,<0.12"
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Runtime dependencies only, exactly as locked.
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python3.12

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY app ./app
# Alembic (M01.02.01): run by the one-shot `api-migrate` compose service as the admin role.
COPY alembic.ini ./
COPY migrations ./migrations
USER app:app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=6 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status == 200 else 1)"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
