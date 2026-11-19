"""Helpers for the M03 foundation tests: seeded scopes, job rows, worker transactions."""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic.config import Config
from compose_support import ComposeProject
from m01_seed import Tenant, seed_tenant
from m02_seed import insert_version, seed_document
from sqlalchemy import Connection, Engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from app.modules.ingestion.ids import PIPELINE_VERSION, revision_id_for

SPACE = "eb3ad37f8d916"
API_DIR = Path(__file__).resolve().parents[3] / "apps" / "api"


def make_url(stack: ComposeProject, role: str) -> URL:
    """Connection URL of ``admin``, ``app`` or ``worker`` on the stack's published port."""
    env = stack.env
    user, password = {
        "admin": (env["POSTGRES_USER"], env["POSTGRES_PASSWORD"]),
        "app": (env["POSTGRES_APP_USER"], env["POSTGRES_APP_PASSWORD"]),
        "worker": (env["POSTGRES_WORKER_USER"], env["POSTGRES_WORKER_PASSWORD"]),
    }[role]
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host="127.0.0.1",
        port=int(env["POSTGRES_HOST_PORT"]),
        database=env["POSTGRES_DB"],
    )


def alembic_cfg(stack: ComposeProject, *x: str) -> Config:
    """An Alembic config with explicit ``-x`` arguments; by default the worker password (taken from
    the environment contract) is passed this way, never through the environment variable."""
    cfg = Config(str(API_DIR / "alembic.ini"))
    args = list(x) or [f"worker_password={stack.env['POSTGRES_WORKER_PASSWORD']}"]
    cfg.cmd_opts = argparse.Namespace(x=args)
    return cfg


@dataclass(frozen=True)
class Scope:
    """One org with a workspace, one document and its first version (admin-seeded)."""

    tenant: Tenant
    document_id: uuid.UUID
    version_id: uuid.UUID

    @property
    def org_id(self) -> uuid.UUID:
        return self.tenant.org_id

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.tenant.workspace_id

    @property
    def owner_id(self) -> uuid.UUID:
        return self.tenant.owner_id


def seed_scope(engine: Engine) -> Scope:
    tenant = seed_tenant(engine, documents=False)
    with engine.begin() as conn:
        _, doc, ver = seed_document(
            conn,
            org_id=tenant.org_id,
            workspace_id=tenant.workspace_id,
            created_by=tenant.owner_id,
        )
    return Scope(tenant, doc, ver)


def add_version(engine: Engine, scope: Scope, number: int) -> uuid.UUID:
    """Another version of the scope's document (admin)."""
    with engine.begin() as conn:
        return insert_version(
            conn,
            org_id=scope.org_id,
            workspace_id=scope.workspace_id,
            document_id=scope.document_id,
            created_by=scope.owner_id,
            version_number=number,
        )


def new_space() -> str:
    return "e" + uuid.uuid4().hex[:12]


def insert_job(
    conn: Connection,
    scope: Scope,
    *,
    state: str = "queued",
    attempts: int = 0,
    max_attempts: int = 5,
    run_after_seconds: int = 0,
    lease_owner: str | None = None,
    lease_expires_in: int | None = None,
    lease_epoch: int = 0,
    version_id: uuid.UUID | None = None,
    space: str | None = None,
) -> uuid.UUID:
    """An ``ingestion_jobs`` row (admin; bypasses RLS). A unique embedding space per call makes
    every job's ``revision_id`` distinct even for the same version."""
    job_id = uuid.uuid4()
    version = version_id or scope.version_id
    space_id = space or new_space()
    conn.execute(
        text(
            "INSERT INTO ingestion_jobs (id, org_id, workspace_id, document_id, version_id,"
            " revision_id, pipeline_version, embedding_space_id, state, attempts, max_attempts,"
            " run_after, lease_owner, lease_expires_at, lease_epoch)"
            " VALUES (:id, :o, :w, :d, :v, :r, :pv, :sp, :st, :at, :ma,"
            " now() + make_interval(secs => :ra), :lo,"
            " CASE WHEN CAST(:le AS integer) IS NULL THEN NULL"
            "      ELSE now() + make_interval(secs => CAST(:le AS integer)) END, :ep)"
        ),
        {
            "id": job_id,
            "o": scope.org_id,
            "w": scope.workspace_id,
            "d": scope.document_id,
            "v": version,
            "r": revision_id_for(version, PIPELINE_VERSION, space_id),
            "pv": PIPELINE_VERSION,
            "sp": space_id,
            "st": state,
            "at": attempts,
            "ma": max_attempts,
            "ra": run_after_seconds,
            "lo": lease_owner,
            "le": lease_expires_in,
            "ep": lease_epoch,
        },
    )
    return job_id


def insert_revision(
    conn: Connection, scope: Scope, *, state: str = "building", version_id: uuid.UUID | None = None
) -> uuid.UUID:
    version = version_id or scope.version_id
    space_id = new_space()
    rev = revision_id_for(version, PIPELINE_VERSION, space_id)
    conn.execute(
        text(
            "INSERT INTO knowledge_revisions (id, org_id, workspace_id, document_id, version_id,"
            " pipeline_version, embedding_space_id, state)"
            " VALUES (:r, :o, :w, :d, :v, :pv, :sp, :st)"
        ),
        {
            "r": rev,
            "o": scope.org_id,
            "w": scope.workspace_id,
            "d": scope.document_id,
            "v": version,
            "pv": PIPELINE_VERSION,
            "sp": space_id,
            "st": state,
        },
    )
    return rev


def insert_outbox(
    conn: Connection,
    scope: Scope,
    *,
    event_type: str = "ingestion.job.queued",
    available_in: int = 0,
    dispatched: bool = False,
    aggregate_id: uuid.UUID | None = None,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO outbox_events (id, org_id, aggregate_type, aggregate_id, event_type,"
            " payload, available_at, dispatched_at)"
            " VALUES (:id, :o, 'ingestion_job', :a, :t, '{}'::jsonb,"
            " now() + make_interval(secs => :av), CASE WHEN :dis THEN now() END)"
        ),
        {
            "id": event_id,
            "o": scope.org_id,
            "a": aggregate_id or uuid.uuid4(),
            "t": event_type,
            "av": available_in,
            "dis": dispatched,
        },
    )
    return event_id


def insert_publication(
    conn: Connection, scope: Scope, revision_id: uuid.UUID, *, reconciled_ago: int | None = None
) -> None:
    conn.execute(
        text(
            "INSERT INTO document_publications (document_id, org_id, workspace_id, revision_id,"
            " version_id, version_number, reconciled_at)"
            " VALUES (:d, :o, :w, :r, :v, 1,"
            " CASE WHEN CAST(:ago AS integer) IS NULL THEN NULL"
            "      ELSE now() - make_interval(secs => CAST(:ago AS integer)) END)"
        ),
        {
            "d": scope.document_id,
            "o": scope.org_id,
            "w": scope.workspace_id,
            "r": revision_id,
            "v": scope.version_id,
            "ago": reconciled_ago,
        },
    )


def set_org(conn: Connection, org_id: uuid.UUID | None) -> None:
    conn.execute(
        text("SELECT set_config('app.current_org_id', :o, true)"),
        {"o": "" if org_id is None else str(org_id)},
    )


@contextmanager
def role_tx(engine: Engine, org_id: uuid.UUID | None) -> Iterator[Connection]:
    """One transaction on ``engine`` (worker or app role) with the org context set; commits at the
    end unless the body raises."""
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            set_org(conn, org_id)
            yield conn
        except BaseException:
            trans.rollback()
            raise
        else:
            trans.commit()


def scalar(conn: Connection, sql: str, **params: Any) -> Any:
    return conn.execute(text(sql), params).scalar_one()


def rows(conn: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def sqlstate(exc: BaseException) -> str | None:
    if isinstance(exc, DBAPIError):
        return getattr(exc.orig, "sqlstate", None)
    return None


def constraint(exc: BaseException) -> str | None:
    if isinstance(exc, DBAPIError):
        diag = getattr(exc.orig, "diag", None)
        return getattr(diag, "constraint_name", None)
    return None


def claim(
    conn: Connection, job_id: uuid.UUID, owner: str = "w1", lease_seconds: int = 60
) -> list[dict[str, Any]]:
    return rows(
        conn, "SELECT * FROM ingest_claim_job(:j, :o, :l)", j=job_id, o=owner, l=lease_seconds
    )
