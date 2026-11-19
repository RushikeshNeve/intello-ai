"""Document / ingestion side of the RLS world (M02 + M03 tables) and the per-role grant registry.

Seeds, as the admin role, one fully populated document scope per tenant of :class:`rls_world.World`
(source, two documents with versions, upload session, jobs, event, revisions, checkpoint,
publication, outbox). ``GRANTS`` is the hand-written expectation of what each of the two
least-privilege roles (``app`` = ``ukos_app``, ``worker`` = ``ukos_worker``) may do on each table;
``test_10_docs_catalog.py`` cross-checks it against the live catalog so the registry cannot rot.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from rls_world import Tenant, World, set_ctx
from sqlalchemy import Connection, Engine, text

SHA = "a" * 64

DOC_TABLES: tuple[str, ...] = (
    "sources",
    "documents",
    "document_versions",
    "upload_sessions",
    "ingestion_jobs",
    "ingestion_job_events",
    "ingestion_checkpoints",
    "knowledge_revisions",
    "document_publications",
    "outbox_events",
)
ROLES: tuple[str, ...] = ("app", "worker")
CASES: list[tuple[str, str]] = [(r, t) for r in ROLES for t in DOC_TABLES]
CASE_IDS = [f"{r}-{t}" for r, t in CASES]

# Primary key columns (for "lookup by PK" attacks).
PK: dict[str, tuple[str, ...]] = {
    "sources": ("id",),
    "documents": ("id",),
    "document_versions": ("id",),
    "upload_sessions": ("id",),
    "ingestion_jobs": ("id",),
    "ingestion_job_events": ("seq",),
    "ingestion_checkpoints": ("revision_id", "stage"),
    "knowledge_revisions": ("id",),
    "document_publications": ("document_id",),
    "outbox_events": ("id",),
}


@dataclass(frozen=True)
class Grant:
    select: bool = False
    insert: bool = False
    update: frozenset[str] = frozenset()


def _g(select: bool = False, insert: bool = False, update: str = "") -> Grant:
    return Grant(select, insert, frozenset(update.split()))


# Migration 0003 (app role) and 0005 (app + worker). No DELETE/TRUNCATE/REFERENCES/TRIGGER anywhere.
GRANTS: dict[str, dict[str, Grant]] = {
    "app": {
        "sources": _g(
            True,
            True,
            "state revoked_at next_sync_at sync_interval_seconds webhook_secret_ciphertext"
            " webhook_key_id",
        ),
        "documents": _g(True, True, "title latest_version_id updated_at deleted_at deleted_by"),
        "document_versions": _g(True, True, "purged_at"),
        "upload_sessions": _g(True, True, "state failure_code finalized_at"),
        "ingestion_jobs": _g(True, True),
        "ingestion_job_events": _g(True, True),
        "ingestion_checkpoints": _g(True),
        "knowledge_revisions": _g(True),
        "document_publications": _g(True),
        "outbox_events": _g(True, True),
    },
    "worker": {
        "sources": _g(True, False, "sync_cursor next_sync_at state"),
        "documents": _g(True, True, "latest_version_id updated_at deleted_at deleted_by"),
        "document_versions": _g(True, True),
        "upload_sessions": _g(),
        "ingestion_jobs": _g(
            True,
            True,
            "state run_after lease_owner lease_expires_at heartbeat_at current_stage"
            " last_error_code last_error_detail updated_at started_at finished_at",
        ),
        "ingestion_job_events": _g(True, True),
        "ingestion_checkpoints": _g(
            True,
            True,
            "state input_fingerprint artifact_key artifact_sha256 artifact_bytes attempts"
            " started_at completed_at error_code",
        ),
        "knowledge_revisions": _g(
            True,
            True,
            "state chunk_count content_fingerprint ready_at published_at superseded_at purged_at",
        ),
        "document_publications": _g(
            True,
            True,
            "revision_id version_id version_number previous_revision_id published_at reconciled_at",
        ),
        "outbox_events": _g(True, True),
    },
}


@dataclass(frozen=True)
class DocScope:
    """Everything seeded for one tenant. ``doc``/``version`` are published; ``doc2`` is not."""

    tenant: Tenant
    source: uuid.UUID
    doc: uuid.UUID
    version: uuid.UUID
    doc2: uuid.UUID
    version2: uuid.UUID
    session: uuid.UUID
    job: uuid.UUID  # queued
    running_job: uuid.UUID  # running, lease already expired
    revision: uuid.UUID  # published, doc/version
    revision2: uuid.UUID  # ready, doc2/version2 (no publication yet)
    outbox: uuid.UUID
    event_seq: int
    title_marker: str

    @property
    def org(self) -> uuid.UUID:
        return self.tenant.org_id

    @property
    def ws(self) -> uuid.UUID:
        return self.tenant.workspace_id

    @property
    def owner(self) -> uuid.UUID:
        return self.tenant.owner_id

    def ids(self) -> dict[str, uuid.UUID]:
        return {
            "ws": self.ws,
            "source": self.source,
            "doc": self.doc,
            "version": self.version,
            "doc2": self.doc2,
            "version2": self.version2,
            "job": self.job,
            "revision2": self.revision2,
        }

    def pk(self, table: str) -> dict[str, Any]:
        """Primary key of this tenant's seeded row of ``table`` (column -> value)."""
        keys: dict[str, dict[str, Any]] = {
            "sources": {"id": self.source},
            "documents": {"id": self.doc},
            "document_versions": {"id": self.version},
            "upload_sessions": {"id": self.session},
            "ingestion_jobs": {"id": self.job},
            "ingestion_job_events": {"seq": self.event_seq},
            "ingestion_checkpoints": {"revision_id": self.revision, "stage": "extract"},
            "knowledge_revisions": {"id": self.revision},
            "document_publications": {"document_id": self.doc},
            "outbox_events": {"id": self.outbox},
        }
        return keys[table]


@dataclass(frozen=True)
class DocWorld:
    a: DocScope
    b: DocScope

    def of(self, org: uuid.UUID) -> DocScope:
        return self.a if org == self.a.org else self.b


def _seed_scope(conn: Any, t: Tenant) -> DocScope:
    u = uuid.uuid4
    source, doc, ver, doc2, ver2 = u(), u(), u(), u(), u()
    session, job, running, rev, rev2, outbox = u(), u(), u(), u(), u(), u()
    marker = f"doc-{t.marker}"
    conn.execute(
        text(
            "INSERT INTO sources (id, org_id, workspace_id, kind, name, created_by)"
            " VALUES (:s, :o, :w, 'upload', 'Uploads', :u)"
        ),
        {"s": source, "o": t.org_id, "w": t.workspace_id, "u": t.owner_id},
    )
    for d, v, title in ((doc, ver, marker), (doc2, ver2, f"{marker}-2")):
        conn.execute(
            text(
                "INSERT INTO documents (id, org_id, workspace_id, source_id, title, created_by)"
                " VALUES (:d, :o, :w, :s, :t, :u)"
            ),
            {"d": d, "o": t.org_id, "w": t.workspace_id, "s": source, "t": title, "u": t.owner_id},
        )
        conn.execute(
            text(
                "INSERT INTO document_versions (id, org_id, workspace_id, document_id,"
                " version_number, original_filename, content_type, size_bytes, sha256, object_key,"
                " created_by) VALUES (:v, :o, :w, :d, 1, 'f.pdf', 'application/pdf', 10, :sha,"
                " :k, :u)"
            ),
            {
                "v": v,
                "o": t.org_id,
                "w": t.workspace_id,
                "d": d,
                "sha": SHA,
                "k": f"org/{t.org_id}/{v}",
                "u": t.owner_id,
            },
        )
        conn.execute(
            text("UPDATE documents SET latest_version_id = :v WHERE id = :d"), {"v": v, "d": d}
        )
    conn.execute(
        text(
            "INSERT INTO upload_sessions (id, org_id, workspace_id, document_id, version_id,"
            " created_by, filename, content_type, size_bytes, sha256, object_key, expires_at)"
            " VALUES (:i, :o, :w, :d, :v, :u, 'f.pdf', 'application/pdf', 10, :sha, :k,"
            " now() + interval '1 hour')"
        ),
        {
            "i": session,
            "o": t.org_id,
            "w": t.workspace_id,
            "d": u(),
            "v": u(),
            "u": t.owner_id,
            "sha": SHA,
            "k": f"org/{t.org_id}/{u()}",
        },
    )
    for r, d, v, state in ((rev, doc, ver, "published"), (rev2, doc2, ver2, "ready")):
        conn.execute(
            text(
                "INSERT INTO knowledge_revisions (id, org_id, workspace_id, document_id,"
                " version_id, pipeline_version, embedding_space_id, state)"
                " VALUES (:r, :o, :w, :d, :v, 'p1', :sp, :st)"
            ),
            {
                "r": r,
                "o": t.org_id,
                "w": t.workspace_id,
                "d": d,
                "v": v,
                "sp": f"sp-{r.hex[:8]}",
                "st": state,
            },
        )
    conn.execute(
        text(
            "INSERT INTO ingestion_checkpoints (revision_id, stage, org_id, input_fingerprint)"
            " VALUES (:r, 'extract', :o, 'fp')"
        ),
        {"r": rev, "o": t.org_id},
    )
    conn.execute(
        text(
            "INSERT INTO document_publications (document_id, org_id, workspace_id, revision_id,"
            " version_id, version_number) VALUES (:d, :o, :w, :r, :v, 1)"
        ),
        {"d": doc, "o": t.org_id, "w": t.workspace_id, "r": rev, "v": ver},
    )
    for jid, state, owner, lease in (
        (job, "queued", None, None),
        (running, "running", "w-old", "now() - interval '1 hour'"),
    ):
        conn.execute(
            text(
                "INSERT INTO ingestion_jobs (id, org_id, workspace_id, document_id, version_id,"
                " revision_id, pipeline_version, embedding_space_id, state, lease_owner,"
                " lease_expires_at, attempts) VALUES (:j, :o, :w, :d, :v, :r, 'p1', :sp, :st,"
                f" :lo, {lease or 'NULL'}, :at)"
            ),
            {
                "j": jid,
                "o": t.org_id,
                "w": t.workspace_id,
                "d": doc2,
                "v": ver2,
                "r": u(),
                "sp": f"sp-{jid.hex[:8]}",
                "st": state,
                "lo": owner,
                "at": 1 if owner else 0,
            },
        )
    seq = conn.execute(
        text(
            "INSERT INTO ingestion_job_events (job_id, org_id, workspace_id, document_id,"
            " version_id, event_type) VALUES (:j, :o, :w, :d, :v, 'queued') RETURNING seq"
        ),
        {"j": job, "o": t.org_id, "w": t.workspace_id, "d": doc2, "v": ver2},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO outbox_events (id, org_id, aggregate_type, aggregate_id, event_type)"
            " VALUES (:i, :o, 'ingestion_job', :a, 'ingestion.job.queued')"
        ),
        {"i": outbox, "o": t.org_id, "a": job},
    )
    return DocScope(
        tenant=t,
        source=source,
        doc=doc,
        version=ver,
        doc2=doc2,
        version2=ver2,
        session=session,
        job=job,
        running_job=running,
        revision=rev,
        revision2=rev2,
        outbox=outbox,
        event_seq=int(seq),
        title_marker=marker,
    )


def seed_doc_world(engine: Engine, world: World) -> DocWorld:
    with engine.begin() as conn:
        a = _seed_scope(conn, world.a)
        b = _seed_scope(conn, world.b)
    # Planner statistics must exist for these tables too (pg_stats must still not expose them).
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for table in DOC_TABLES:
            conn.execute(text(f"ANALYZE {table}"))
    return DocWorld(a, b)


# --------------------------------------------------------------------------------------------
# INSERT builders: one per table. ``ids`` names the parents (a DocScope.ids() mapping, possibly
# with one entry swapped for another tenant's), ``org``/``owner`` the row's own org and creator.
# --------------------------------------------------------------------------------------------

Builder = Callable[[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]], tuple[str, dict[str, Any]]]


def _b_sources(
    org: uuid.UUID, owner: uuid.UUID, p: dict[str, uuid.UUID]
) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO sources (id, org_id, workspace_id, kind, name, created_by)"
        " VALUES (:id, :o, :ws, 'url', 'evil', :u)",
        {"id": uuid.uuid4(), "o": org, "ws": p["ws"], "u": owner},
    )


def _b_documents(
    org: uuid.UUID, owner: uuid.UUID, p: dict[str, uuid.UUID]
) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO documents (id, org_id, workspace_id, source_id, title, created_by)"
        " VALUES (:id, :o, :ws, :src, 'evil', :u)",
        {"id": uuid.uuid4(), "o": org, "ws": p["ws"], "src": p["source"], "u": owner},
    )


def _b_versions(
    org: uuid.UUID, owner: uuid.UUID, p: dict[str, uuid.UUID]
) -> tuple[str, dict[str, Any]]:
    vid = uuid.uuid4()
    return (
        "INSERT INTO document_versions (id, org_id, workspace_id, document_id, version_number,"
        " original_filename, content_type, size_bytes, sha256, object_key, created_by)"
        " VALUES (:id, :o, :ws, :d, 2, 'evil.pdf', 'application/pdf', 1, :sha, :k, :u)",
        {
            "id": vid,
            "o": org,
            "ws": p["ws"],
            "d": p["doc2"],
            "sha": SHA,
            "k": f"evil/{vid}",
            "u": owner,
        },
    )


def _b_sessions(
    org: uuid.UUID, owner: uuid.UUID, p: dict[str, uuid.UUID]
) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO upload_sessions (id, org_id, workspace_id, document_id, version_id,"
        " created_by, filename, content_type, size_bytes, sha256, object_key, expires_at)"
        " VALUES (:id, :o, :ws, :d, :v, :u, 'evil.pdf', 'application/pdf', 1, :sha, :k,"
        " now() + interval '1 hour')",
        {
            "id": uuid.uuid4(),
            "o": org,
            "ws": p["ws"],
            "d": uuid.uuid4(),
            "v": uuid.uuid4(),
            "u": owner,
            "sha": SHA,
            "k": f"evil/{uuid.uuid4()}",
        },
    )


def _b_jobs(
    org: uuid.UUID, owner: uuid.UUID, p: dict[str, uuid.UUID]
) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO ingestion_jobs (id, org_id, workspace_id, document_id, version_id,"
        " revision_id, pipeline_version, embedding_space_id)"
        " VALUES (:id, :o, :ws, :d, :v, :r, 'p1', 'evil')",
        {
            "id": uuid.uuid4(),
            "o": org,
            "ws": p["ws"],
            "d": p["doc2"],
            "v": p["version2"],
            "r": uuid.uuid4(),
        },
    )


def _b_events(
    org: uuid.UUID, owner: uuid.UUID, p: dict[str, uuid.UUID]
) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO ingestion_job_events (job_id, org_id, workspace_id, document_id, version_id,"
        " event_type) VALUES (:j, :o, :ws, :d, :v, 'queued')",
        {"j": p["job"], "o": org, "ws": p["ws"], "d": p["doc2"], "v": p["version2"]},
    )


def _b_checkpoints(
    org: uuid.UUID, owner: uuid.UUID, p: dict[str, uuid.UUID]
) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO ingestion_checkpoints (revision_id, stage, org_id, input_fingerprint)"
        " VALUES (:r, 'chunk', :o, 'evil')",
        {"r": p["revision2"], "o": org},
    )


def _b_revisions(
    org: uuid.UUID, owner: uuid.UUID, p: dict[str, uuid.UUID]
) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO knowledge_revisions (id, org_id, workspace_id, document_id, version_id,"
        " pipeline_version, embedding_space_id)"
        " VALUES (:id, :o, :ws, :d, :v, 'p1', :sp)",
        {
            "id": uuid.uuid4(),
            "o": org,
            "ws": p["ws"],
            "d": p["doc2"],
            "v": p["version2"],
            "sp": f"evil-{uuid.uuid4().hex[:8]}",
        },
    )


def _b_publications(
    org: uuid.UUID, owner: uuid.UUID, p: dict[str, uuid.UUID]
) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO document_publications (document_id, org_id, workspace_id, revision_id,"
        " version_id, version_number) VALUES (:d, :o, :ws, :r, :v, 1)",
        {"d": p["doc2"], "o": org, "ws": p["ws"], "r": p["revision2"], "v": p["version2"]},
    )


def _b_outbox(
    org: uuid.UUID, owner: uuid.UUID, p: dict[str, uuid.UUID]
) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO outbox_events (org_id, aggregate_type, aggregate_id, event_type)"
        " VALUES (:o, 'evil', :a, 'knowledge.reconcile')",
        {"o": org, "a": uuid.uuid4()},
    )


BUILDERS: dict[str, Builder] = {
    "sources": _b_sources,
    "documents": _b_documents,
    "document_versions": _b_versions,
    "upload_sessions": _b_sessions,
    "ingestion_jobs": _b_jobs,
    "ingestion_job_events": _b_events,
    "ingestion_checkpoints": _b_checkpoints,
    "knowledge_revisions": _b_revisions,
    "document_publications": _b_publications,
    "outbox_events": _b_outbox,
}

# Parent keys (of DocScope.ids()) each builder actually references; each one is a composite-FK pin.
PARENTS: dict[str, tuple[str, ...]] = {
    "sources": ("ws",),
    "documents": ("ws", "source"),
    "document_versions": ("ws", "doc2"),
    "upload_sessions": ("ws",),
    "ingestion_jobs": ("ws", "doc2", "version2"),
    "ingestion_job_events": ("ws", "job", "doc2", "version2"),
    "ingestion_checkpoints": ("revision2",),
    "knowledge_revisions": ("ws", "doc2", "version2"),
    "document_publications": ("ws", "doc2", "version2", "revision2"),
    "outbox_events": (),
}


# --------------------------------------------------------------------------------------------
# Admin-connection helpers: act as a least-privilege role on ONE connection that also holds the
# admin's uncommitted changes (SET LOCAL ROLE), always rolled back.
# --------------------------------------------------------------------------------------------

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


@contextmanager
def admin_tx(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            yield conn
        finally:
            if trans.is_active:
                trans.rollback()


def become(conn: Connection, role_name: str, org: uuid.UUID | None = None) -> None:
    """``SET LOCAL ROLE`` to the least-privilege role and set its transaction-local org context."""
    conn.execute(text(f'SET LOCAL ROLE "{role_name}"'))
    set_ctx(conn, org=org)


def pk_where(table: str, prefix: str = "pk_") -> str:
    return " AND ".join(f"{c} = :{prefix}{c}" for c in PK[table])


def pk_params(pk: dict[str, Any], prefix: str = "pk_") -> dict[str, Any]:
    return {f"{prefix}{c}": v for c, v in pk.items()}


# Tables whose INSERT can collide on a client-chosen key (ON CONFLICT attacks); None = no such key.
PK_CONFLICT: dict[str, tuple[str, ...] | None] = {
    "sources": ("id",),
    "documents": ("id",),
    "document_versions": ("id",),
    "upload_sessions": ("id",),
    "ingestion_jobs": ("id",),
    "ingestion_job_events": None,  # generated identity: nothing to collide on
    "ingestion_checkpoints": ("revision_id", "stage"),
    "knowledge_revisions": ("id",),
    "document_publications": ("document_id",),
    "outbox_events": None,  # id is server-generated in the builder
}


def _cases(pred: Callable[[str, str], bool]) -> list[tuple[str, str]]:
    return [(r, t) for r, t in CASES if pred(r, t)]


# Only the combinations a test can meaningfully run (no skipped placeholders).
SELECT_CASES = _cases(lambda r, t: GRANTS[r][t].select)
INSERT_CASES = _cases(lambda r, t: GRANTS[r][t].insert)
UPDATE_CASES = _cases(lambda r, t: bool(GRANTS[r][t].update))
ORACLE_CASES = _cases(lambda r, t: GRANTS[r][t].insert and bool(PARENTS[t]))
CONFLICT_CASES = _cases(lambda r, t: GRANTS[r][t].insert and PK_CONFLICT[t] is not None)


def ids(cases: list[tuple[str, str]]) -> list[str]:
    return [f"{r}-{t}" for r, t in cases]
