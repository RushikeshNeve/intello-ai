"""Worker-side settings: the shared :class:`~app.core.config.Settings` plus broker/dispatcher knobs.

Kept out of ``app.core.config`` on purpose (the foundation owns that file); every variable here is
optional with a safe default, so the API never needs any of them.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr

from app.core.config import Settings

DEFAULT_PIPELINE_RUNNER = "app.modules.ingestion.pipeline:build_pipeline_runner"
DEFAULT_DELETE_HANDLER = "app.modules.knowledge.deletion:build_document_deleted_handler"
DEFAULT_RECONCILE_HANDLER = "app.modules.knowledge.publication:build_reconcile_handler"


class WorkerSettings(Settings):
    # Redis is a broker only (ADR-036): no job state lives there.
    CELERY_BROKER_URL: SecretStr = SecretStr("redis://127.0.0.1:6479/1")

    # Dotted ``module:factory`` paths. Each factory is called as ``factory(settings)`` and returns
    # an object satisfying the matching Protocol in ``app.workers.pipeline_port``.
    INGEST_PIPELINE_RUNNER: str = DEFAULT_PIPELINE_RUNNER
    INGEST_DELETE_HANDLER: str = DEFAULT_DELETE_HANDLER
    INGEST_RECONCILE_HANDLER: str = DEFAULT_RECONCILE_HANDLER

    # Dispatcher.
    OUTBOX_BATCH_SIZE: int = Field(default=50, ge=1, le=1000)
    OUTBOX_MAX_BACKOFF_SECONDS: int = Field(default=300, ge=1, le=86_400)
    OUTBOX_POISON_RETRY_SECONDS: int = Field(default=3600, ge=1, le=7 * 86_400)
    RECONCILE_BATCH_SIZE: int = Field(default=50, ge=1, le=1000)
    DISPATCHER_HEALTH_FILE: str = "/tmp/ukos-dispatcher.alive"  # noqa: S108 - container-local file
    DISPATCHER_HEALTH_MAX_AGE_SECONDS: int = Field(default=60, ge=1, le=3600)

    # Celery: the hard limit sits this many seconds above the soft one.
    INGEST_TASK_HARD_LIMIT_MARGIN_SECONDS: int = Field(default=60, ge=1, le=3600)
    MAINTENANCE_TASK_SOFT_LIMIT_SECONDS: int = Field(default=300, ge=5, le=86_400)

    @property
    def ingest_hard_limit_seconds(self) -> int:
        return self.INGEST_TASK_SOFT_LIMIT_SECONDS + self.INGEST_TASK_HARD_LIMIT_MARGIN_SECONDS

    @property
    def visibility_timeout_seconds(self) -> int:
        """Redis redelivers an unacked message after this long: it must outlast any task."""
        longest = max(self.ingest_hard_limit_seconds, self.MAINTENANCE_TASK_SOFT_LIMIT_SECONDS + 60)
        return longest + 600


@lru_cache
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
