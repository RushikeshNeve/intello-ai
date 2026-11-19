"""Helpers of the M06 connector tests."""

from __future__ import annotations

import uuid
from typing import Any

from catalog_support import World
from sqlalchemy import Engine, text

from app.modules.connectors.sync import SyncDeps, run_source_sync

TOKEN = "ghp_M06_super_secret_token"


def create_source(
    world: World, *, token: str | None = TOKEN, repo: str = "acme/docs", **extra: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {"provider": "github", "repo": repo, **extra}
    if token:
        body["token"] = token
    res = world.api.post(world.path("/sources"), world.ws_admin, body)
    assert res.status == 201, res.body
    return dict(res.body)


def run(deps: SyncDeps, world: World, source_id: str | uuid.UUID) -> str:
    return run_source_sync(deps, world.org_id, uuid.UUID(str(source_id)))


def request_sync(world: World, source_id: str) -> Any:
    return world.api.post(world.path(f"/sources/{source_id}/sync"), world.ws_admin, {})


def rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).all())


def scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def docs_of(engine: Engine, source_id: str) -> dict[str, Any]:
    """title -> row (id, deleted, versions) of every document the source brought in."""
    found = rows(
        engine,
        "SELECT d.id, d.title, d.deleted_at IS NOT NULL AS deleted,"
        " (SELECT count(*) FROM document_versions v WHERE v.document_id = d.id) AS versions"
        " FROM documents d WHERE d.source_id = :s",
        s=uuid.UUID(source_id),
    )
    return {r.title: r for r in found}
