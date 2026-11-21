"""Runtime settings read from the environment contract (`infrastructure/env/.env.example`).

Secrets and connection strings (which embed credentials) are `SecretStr`, so they never show up
in `repr()`, logs or validation errors.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.modules.ingestion.ids import compute_embedding_space_id


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=True)

    UKOS_ENV: Literal["local", "test", "ci"] = "local"
    UKOS_LOG_LEVEL: Literal["debug", "info", "warning", "error"] = "info"
    # Observability (M08.02.04). Tracing is off by default; JSON logs never carry content.
    UKOS_LOG_FORMAT: Literal["json", "text"] = "json"
    OTEL_ENABLED: bool = False
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://otel-collector:4318"
    # Internal Prometheus listener (0 = off). Never published on a host port by the compose files.
    METRICS_PORT: int = Field(default=0, ge=0, le=65535)

    DATABASE_URL: SecretStr = SecretStr("postgresql+psycopg://ukos_app@127.0.0.1:5532/ukos")
    # Admin-role connection used only by Alembic (never by the request path; RLS bypass risk).
    DATABASE_MIGRATION_URL: SecretStr = SecretStr(
        "postgresql+psycopg://ukos_admin@127.0.0.1:5532/ukos"
    )
    REDIS_URL: SecretStr = SecretStr("redis://127.0.0.1:6479/0")

    # OIDC bearer-token validation (contract docs/architecture/m01-contracts.md section 1).
    # Tokens carry the public issuer; JWKS is fetched from the compose-internal URL.
    OIDC_ISSUER_URL: str = "http://127.0.0.1:8380/dex"
    OIDC_INTERNAL_URL: str = "http://127.0.0.1:8380/dex"
    OIDC_AUDIENCE: str = "ukos-api"
    OIDC_JWKS_CACHE_SECONDS: int = Field(default=300, ge=0, le=86_400)
    OIDC_LEEWAY_SECONDS: int = Field(default=30, ge=0, le=30)

    # Comma-separated list of allowed CORS origins.
    API_CORS_ORIGINS: str = ""
    API_SECRET_KEY: SecretStr | None = None

    # Uploads and quotas (contract docs/architecture/m02-contracts.md §3, §5).
    UPLOAD_MAX_BYTES: int = Field(default=104_857_600, ge=1, le=5 * 1024**3)  # 100 MiB
    ORG_STORAGE_QUOTA_BYTES: int = Field(default=5_368_709_120, ge=1, le=1024**5)  # 5 GiB
    UPLOAD_SESSION_TTL_SECONDS: int = Field(default=3600, ge=1, le=7 * 86_400)
    UPLOAD_MAX_PENDING_PER_USER: int = Field(default=20, ge=1, le=1000)
    DOCUMENT_PURGE_GRACE_DAYS: int = Field(default=7, ge=1, le=3650)

    # Worker role (M03 contract §4): created by migration 0005, used by the dispatcher and Celery
    # workers only. The API never needs it.
    POSTGRES_WORKER_USER: str = Field(default="ukos_worker", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    POSTGRES_WORKER_PASSWORD: SecretStr | None = None
    DATABASE_WORKER_URL: SecretStr | None = None

    # Ingestion pipeline (M03 contract §6, §7, §10).
    INGEST_MAX_ATTEMPTS: int = Field(default=5, ge=1, le=50)
    INGEST_LEASE_SECONDS: int = Field(default=120, ge=5, le=3600)
    INGEST_HEARTBEAT_SECONDS: int = Field(default=30, ge=1, le=1800)
    INGEST_RETRY_BASE_SECONDS: int = Field(default=10, ge=0, le=3600)
    INGEST_CONCURRENCY: int = Field(default=2, ge=1, le=64)
    INGEST_TASK_SOFT_LIMIT_SECONDS: int = Field(default=900, ge=10, le=86_400)
    INGEST_REAP_INTERVAL_SECONDS: int = Field(default=30, ge=1, le=3600)
    INGEST_RECONCILE_INTERVAL_SECONDS: int = Field(default=300, ge=5, le=86_400)
    INGEST_MAX_PDF_PAGES: int = Field(default=2000, ge=1, le=100_000)
    INGEST_MAX_EXTRACT_BYTES: int = Field(default=52_428_800, ge=1024, le=2 * 1024**3)
    INGEST_MAX_CHUNKS: int = Field(default=20_000, ge=1, le=1_000_000)
    INGEST_CHUNK_TARGET_CHARS: int = Field(default=1800, ge=200, le=2400)
    INGEST_CHUNK_OVERLAP_CHARS: int = Field(default=200, ge=0, le=1200)
    INGEST_EMBED_BATCH_SIZE: int = Field(default=16, ge=1, le=512)
    OUTBOX_POLL_SECONDS: float = Field(default=1.0, gt=0, le=60)

    # Embedding space identity (M03 contract §7, ADR-020): changing ANY of these five values is a
    # new embedding space, hence a new revision, a new vector index and re-embedding.
    AI_EMBEDDING_MODEL: str = Field(default="google/embedding-gemma-300m", min_length=1)
    AI_EMBEDDING_DIMENSIONS: int = Field(default=768, ge=1, le=8192)
    AI_EMBEDDING_QUERY_PREFIX: str = "task: search result | query: "
    AI_EMBEDDING_DOCUMENT_PREFIX: str = "title: none | text: "
    AI_EMBEDDING_SPEC_VERSION: int = Field(default=1, ge=1)

    # Search, chat and verification (M04 contract §11).
    SEARCH_DEFAULT_LIMIT: int = Field(default=10, ge=1, le=50)
    SEARCH_CANDIDATES: int = Field(default=50, ge=1, le=500)
    SEARCH_MAX_PER_DOCUMENT: int = Field(default=3, ge=1, le=20)
    SEARCH_RATE_PER_MIN: int = Field(default=60, ge=1, le=100_000)
    CHAT_MODEL: str = Field(default="google/gemma-3-4b", min_length=1, max_length=200)
    CHAT_CONTEXT_TOKENS: int = Field(default=4096, ge=256, le=1_000_000)
    CHAT_MAX_OUTPUT_TOKENS: int = Field(default=800, ge=1, le=65_536)
    CHAT_TEMPERATURE: float = Field(default=0.0, ge=0.0, le=2.0)
    CHAT_HISTORY_TURNS: int = Field(default=6, ge=0, le=50)
    CHAT_RUN_TIMEOUT_SECONDS: int = Field(default=180, ge=10, le=3600)
    CHAT_MAX_RUNS_PER_MIN: int = Field(default=10, ge=1, le=10_000)
    CHAT_MAX_CONCURRENT_RUNS: int = Field(default=2, ge=1, le=100)
    # Chat runtime (M04.02.04): run lease/heartbeat like ingestion (a dead worker's run is reaped
    # when the lease lapses), retrieval timeout (contract §8: 10 s) and evidence per answer.
    CHAT_LEASE_SECONDS: int = Field(default=60, ge=5, le=3600)
    CHAT_HEARTBEAT_SECONDS: int = Field(default=15, ge=1, le=1800)
    CHAT_RETRIEVAL_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, le=300)
    CHAT_EVIDENCE_TOP_K: int = Field(default=8, ge=1, le=20)
    VERIFY_MODE: Literal["strict", "flag"] = "strict"
    VERIFY_LEXICAL_MIN: float = Field(default=0.35, ge=0.0, le=1.0)
    VERIFY_EMBED_MIN: float = Field(default=0.55, ge=0.0, le=1.0)
    # External providers (DC-020): off unless BOTH this flag and the org's ai_policies row allow it.
    AI_EXTERNAL_PROVIDERS_ENABLED: bool = False
    OPENAI_API_KEY: SecretStr | None = None
    OPENAI_CHAT_MODEL: str = Field(default="gpt-4o-mini", min_length=1, max_length=200)

    # Connectors (M06 contract section 5). The key is base64 of 32 bytes; it is needed only when a
    # token or a webhook secret is stored. ``..._OLD_KEYS`` is ``key_id:base64,key_id:base64`` and
    # only decrypts (rotation: new key active, old ids kept until rows are re-encrypted).
    CONNECTOR_ENCRYPTION_KEY: SecretStr | None = None
    CONNECTOR_ENCRYPTION_KEY_ID: str = Field(default="k1", min_length=1, max_length=32)
    CONNECTOR_ENCRYPTION_OLD_KEYS: SecretStr | None = None
    CONNECTOR_MAX_FILE_BYTES: int = Field(default=5_242_880, ge=1024, le=100_000_000)
    CONNECTOR_SYNC_DEFAULT_INTERVAL_SECONDS: int = Field(default=3600, ge=300, le=2_592_000)
    CONNECTOR_SYNC_MAX_FILES_PER_RUN: int = Field(default=500, ge=1, le=100_000)
    CONNECTOR_SCHEDULER_INTERVAL_SECONDS: int = Field(default=30, ge=1, le=3600)
    GITHUB_API_BASE_URL: str = Field(default="https://api.github.com", min_length=8)
    # Usage metering (M11.01). JSON {"provider/model": {"in": n, "out": n, "cached": n}} in micro
    # currency units per 1M tokens; "provider/*" is the fallback. Unlisted = 0 (local models).
    USAGE_PRICES_JSON: str = "{}"
    # Knowledge graph (M05 contract §7). The build task is a no-op while GRAPH_ENABLED is false.
    GRAPH_ENABLED: bool = False
    GRAPH_ROUTER_MODE: Literal["auto", "off", "force"] = "auto"
    GRAPH_MAX_CHUNKS_PER_REVISION: int = Field(default=400, ge=1, le=100_000)
    GRAPH_MAX_ENTITIES_PER_CHUNK: int = Field(default=12, ge=1, le=100)
    GRAPH_EXTRACT_BATCH_CHUNKS: int = Field(default=6, ge=1, le=50)
    GRAPH_TRAVERSAL_MAX_DEPTH: int = Field(default=2, ge=1, le=4)
    GRAPH_MAX_NODES: int = Field(default=50, ge=1, le=500)
    # Memory and research (M07 contract §6). Limits are ceilings; a request can only lower them.
    MEMORY_ENABLED: bool = True
    MEMORY_MAX_PER_PROMPT: int = Field(default=5, ge=1, le=20)
    RESEARCH_MAX_SUBQUERIES: int = Field(default=6, ge=1, le=20)
    RESEARCH_MAX_EVIDENCE: int = Field(default=60, ge=1, le=500)
    RESEARCH_MAX_MODEL_CALLS: int = Field(default=12, ge=2, le=100)
    RESEARCH_TIMEOUT_SECONDS: int = Field(default=600, ge=10, le=7200)
    RESEARCH_LEASE_SECONDS: int = Field(default=60, ge=5, le=3600)

    # Per-dependency timeout for /health/ready, seconds.
    HEALTH_CHECK_TIMEOUT_SECONDS: float = Field(default=2.0, gt=0, le=30)

    @field_validator(
        "OPENAI_API_KEY", "CONNECTOR_ENCRYPTION_KEY", "CONNECTOR_ENCRYPTION_OLD_KEYS", mode="before"
    )
    @classmethod
    def _blank_openai_key_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_chat_settings(self) -> Settings:
        if self.CHAT_MAX_OUTPUT_TOKENS * 2 > self.CHAT_CONTEXT_TOKENS:
            raise ValueError(
                "CHAT_MAX_OUTPUT_TOKENS must be at most half of CHAT_CONTEXT_TOKENS "
                "(the prompt needs room for evidence)"
            )
        if self.SEARCH_DEFAULT_LIMIT > self.SEARCH_CANDIDATES:
            raise ValueError("SEARCH_DEFAULT_LIMIT must not exceed SEARCH_CANDIDATES")
        if self.CHAT_HEARTBEAT_SECONDS * 2 > self.CHAT_LEASE_SECONDS:
            raise ValueError(
                "CHAT_HEARTBEAT_SECONDS must be at most half of CHAT_LEASE_SECONDS "
                "(a lease must survive one missed heartbeat)"
            )
        return self

    @model_validator(mode="after")
    def _check_ingestion_settings(self) -> Settings:
        if self.INGEST_HEARTBEAT_SECONDS * 2 > self.INGEST_LEASE_SECONDS:
            raise ValueError(
                "INGEST_HEARTBEAT_SECONDS must be at most half of INGEST_LEASE_SECONDS "
                "(a lease must survive one missed heartbeat)"
            )
        if self.INGEST_CHUNK_OVERLAP_CHARS * 2 > self.INGEST_CHUNK_TARGET_CHARS:
            raise ValueError("INGEST_CHUNK_OVERLAP_CHARS must be at most half of the chunk target")
        return self

    @property
    def embedding_space_id(self) -> str:
        """The embedding space the current settings describe (``eb3ad37f8d916`` by default)."""
        return compute_embedding_space_id(
            self.AI_EMBEDDING_MODEL,
            self.AI_EMBEDDING_DIMENSIONS,
            self.AI_EMBEDDING_QUERY_PREFIX,
            self.AI_EMBEDDING_DOCUMENT_PREFIX,
            self.AI_EMBEDDING_SPEC_VERSION,
        )

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.API_CORS_ORIGINS.split(",") if o.strip()]

    @property
    def psycopg_dsn(self) -> str:
        """DATABASE_URL as a libpq URI (drops the SQLAlchemy `+psycopg` driver suffix)."""
        return to_libpq_dsn(self.DATABASE_URL.get_secret_value())

    @property
    def sqlalchemy_url(self) -> str:
        """DATABASE_URL with the psycopg 3 driver forced (``postgresql+psycopg://``)."""
        return to_sqlalchemy_url(self.DATABASE_URL.get_secret_value())

    @property
    def sqlalchemy_migration_url(self) -> str:
        """DATABASE_MIGRATION_URL with the psycopg 3 driver forced."""
        return to_sqlalchemy_url(self.DATABASE_MIGRATION_URL.get_secret_value())

    @property
    def sqlalchemy_worker_url(self) -> str | None:
        """DATABASE_WORKER_URL with the psycopg 3 driver forced (``None`` when not configured)."""
        if self.DATABASE_WORKER_URL is None:
            return None
        return to_sqlalchemy_url(self.DATABASE_WORKER_URL.get_secret_value())


def to_libpq_dsn(url: str) -> str:
    """Drop a SQLAlchemy ``+driver`` suffix so libpq/psycopg accept the URI."""
    scheme, sep, rest = url.partition("://")
    if sep and "+" in scheme:
        scheme = scheme.split("+", 1)[0]
    return f"{scheme}{sep}{rest}"


def to_sqlalchemy_url(url: str) -> str:
    """Normalize ``postgres://``, ``postgresql://`` or ``postgresql+x://`` to psycopg 3."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    base = scheme.split("+", 1)[0]
    if base in ("postgres", "postgresql"):
        return f"postgresql+psycopg://{rest}"
    return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
