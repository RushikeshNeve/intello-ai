"""Default dependencies of the sync task: real object store, GitHub over ``httpx`` (M06.01.05).

Tests substitute ``SyncDeps`` through ``app.workers.tasks.set_connector_deps_override``.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import Settings
from app.modules.connectors.github import GitHubConnector
from app.modules.connectors.port import SourceConnector
from app.modules.connectors.sync import ConnectorFactory, SyncDeps
from app.workers.worker_db import get_worker_engine

USER_AGENT = "IntelloBot"


def github_client(base_url: str, token: str | None) -> httpx.Client:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        base_url=base_url.rstrip("/"), headers=headers, timeout=20.0, follow_redirects=False
    )


def default_connector_factory(settings: Settings) -> ConnectorFactory:
    def build(config: dict[str, Any], token: str | None) -> SourceConnector:
        return GitHubConnector(
            github_client(settings.GITHUB_API_BASE_URL, token),
            config,
            max_file_bytes=settings.CONNECTOR_MAX_FILE_BYTES,
            max_files=settings.CONNECTOR_SYNC_MAX_FILES_PER_RUN,
        )

    return build


def default_deps(settings: Settings) -> SyncDeps:
    from app.modules.ingestion import wiring

    return SyncDeps(
        engine=get_worker_engine(settings),
        settings=settings,
        store=wiring.default_uploads_store(),
        connector_factory=default_connector_factory(settings),
    )
