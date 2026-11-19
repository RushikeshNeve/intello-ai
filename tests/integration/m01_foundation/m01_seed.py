"""Seeding helpers (run as the admin role, which bypasses RLS)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError


def new_slug(prefix: str = "org") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def insert_user(conn: Connection, *, subject: str | None = None) -> uuid.UUID:
    user_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (id, issuer, subject, email, display_name)"
            " VALUES (:id, 'http://issuer.test', :sub, :email, 'Test User')"
        ),
        {"id": user_id, "sub": subject or f"sub-{user_id}", "email": f"{user_id}@example.test"},
    )
    return user_id


def insert_org(conn: Connection, *, created_by: uuid.UUID, slug: str | None = None) -> uuid.UUID:
    org_id = uuid.uuid4()
    conn.execute(
        text("INSERT INTO organizations (id, slug, name, created_by) VALUES (:id, :s, 'Org', :u)"),
        {"id": org_id, "s": slug or new_slug(), "u": created_by},
    )
    return org_id


def add_member(conn: Connection, org_id: uuid.UUID, user_id: uuid.UUID, role: str) -> None:
    conn.execute(
        text("INSERT INTO organization_memberships (org_id, user_id, role) VALUES (:o, :u, :r)"),
        {"o": org_id, "u": user_id, "r": role},
    )


def insert_workspace(conn: Connection, org_id: uuid.UUID, created_by: uuid.UUID) -> uuid.UUID:
    ws_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO workspaces (id, org_id, slug, name, created_by)"
            " VALUES (:id, :o, :s, 'Workspace', :u)"
        ),
        {"id": ws_id, "o": org_id, "s": new_slug("ws"), "u": created_by},
    )
    return ws_id


@dataclass(frozen=True)
class Tenant:
    org_id: uuid.UUID
    owner_id: uuid.UUID
    workspace_id: uuid.UUID


def seed_tenant(engine: Engine, *, documents: bool = True) -> Tenant:
    """A complete org: owner, workspace, workspace membership, grant and audit event.

    ``documents=True`` also seeds one M02 source/document/version/upload session; M02 tests that
    need an empty workspace pass ``documents=False``.
    """
    with engine.begin() as conn:
        owner = insert_user(conn)
        org = insert_org(conn, created_by=owner)
        add_member(conn, org, owner, "owner")
        ws = insert_workspace(conn, org, owner)
        conn.execute(
            text(
                "INSERT INTO workspace_memberships (workspace_id, org_id, user_id, role)"
                " VALUES (:w, :o, :u, 'admin')"
            ),
            {"w": ws, "o": org, "u": owner},
        )
        conn.execute(
            text(
                "INSERT INTO resource_grants (id, org_id, workspace_id, resource_type, resource_id,"
                " grantee_type, grantee_id, permission, granted_by)"
                " VALUES (:id, :o, :w, 'document', :r, 'workspace_role', 'viewer', 'read', :u)"
            ),
            {"id": uuid.uuid4(), "o": org, "w": ws, "r": uuid.uuid4(), "u": owner},
        )
        conn.execute(
            text(
                "INSERT INTO audit_events (id, org_id, actor_user_id, event_type, target_type,"
                " target_id, outcome) VALUES (:id, :o, :u, 'org.created', 'org', :t, 'success')"
            ),
            {"id": uuid.uuid4(), "o": org, "u": owner, "t": str(org)},
        )
        # M02 tables (migration 0003): one source/document/version/upload session per tenant, so
        # "every org-scoped table has rows" holds for the whole schema.
        if documents:
            source, doc, version = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO sources (id, org_id, workspace_id, kind, name, created_by)"
                    " VALUES (:s, :o, :w, 'upload', 'Uploads', :u)"
                ),
                {"s": source, "o": org, "w": ws, "u": owner},
            )
            conn.execute(
                text(
                    "INSERT INTO documents (id, org_id, workspace_id, source_id, title, created_by)"
                    " VALUES (:d, :o, :w, :s, 'Seed document', :u)"
                ),
                {"d": doc, "o": org, "w": ws, "s": source, "u": owner},
            )
            key = f"org/{org}/doc/{doc}/{version}/original"
            conn.execute(
                text(
                    "INSERT INTO document_versions (id, org_id, workspace_id, document_id,"
                    " version_number, original_filename, content_type, size_bytes, sha256,"
                    " object_key, created_by) VALUES (:v, :o, :w, :d, 1, 'seed.txt', 'text/plain',"
                    " 4, :sha, :k, :u)"
                ),
                {"v": version, "o": org, "w": ws, "d": doc, "sha": "a" * 64, "k": key, "u": owner},
            )
            conn.execute(
                text("UPDATE documents SET latest_version_id = :v WHERE id = :d"),
                {"v": version, "d": doc},
            )
            conn.execute(
                text(
                    "INSERT INTO upload_sessions (id, org_id, workspace_id, document_id, version_id,"
                    " created_by, filename, content_type, size_bytes, sha256, object_key, expires_at)"
                    " VALUES (:i, :o, :w, :d, :v, :u, 'pending.txt', 'text/plain', 4, :sha, :k,"
                    " now() + interval '1 hour')"
                ),
                {
                    "i": uuid.uuid4(),
                    "o": org,
                    "w": ws,
                    "d": uuid.uuid4(),
                    "v": uuid.uuid4(),
                    "u": owner,
                    "sha": "a" * 64,
                    "k": f"org/{org}/doc/pending/{uuid.uuid4()}/original",
                },
            )
            _seed_ingestion(conn, org, ws, doc, version)
    return Tenant(org_id=org, owner_id=owner, workspace_id=ws)


def _seed_ingestion(
    conn: Connection, org: uuid.UUID, ws: uuid.UUID, doc: uuid.UUID, version: uuid.UUID
) -> None:
    """M03 tables (migration 0005): one revision, job + event, checkpoint, publication, outbox row."""
    revision, job = uuid.uuid4(), uuid.uuid4()
    ids = {"r": revision, "j": job, "o": org, "w": ws, "d": doc, "v": version}
    conn.execute(
        text(
            "INSERT INTO knowledge_revisions (id, org_id, workspace_id, document_id, version_id,"
            " pipeline_version, embedding_space_id, state, chunk_count)"
            " VALUES (:r, :o, :w, :d, :v, '1', 'eseed', 'published', 1)"
        ),
        ids,
    )
    conn.execute(
        text(
            "INSERT INTO ingestion_jobs (id, org_id, workspace_id, document_id, version_id,"
            " revision_id, pipeline_version, embedding_space_id)"
            " VALUES (:j, :o, :w, :d, :v, :r, '1', 'eseed')"
        ),
        ids,
    )
    conn.execute(
        text(
            "INSERT INTO ingestion_job_events (job_id, org_id, workspace_id, document_id,"
            " version_id, event_type) VALUES (:j, :o, :w, :d, :v, 'queued')"
        ),
        ids,
    )
    conn.execute(
        text(
            "INSERT INTO ingestion_checkpoints (revision_id, stage, org_id, input_fingerprint)"
            " VALUES (:r, 'extract', :o, 'fp')"
        ),
        ids,
    )
    conn.execute(
        text(
            "INSERT INTO document_publications (document_id, org_id, workspace_id, revision_id,"
            " version_id, version_number) VALUES (:d, :o, :w, :r, :v, 1)"
        ),
        ids,
    )
    conn.execute(
        text(
            "INSERT INTO outbox_events (org_id, aggregate_type, aggregate_id, event_type, payload)"
            " VALUES (:o, 'ingestion_job', :j, 'ingestion.job.queued', '{}'::jsonb)"
        ),
        ids,
    )


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def constraint_name(exc: DBAPIError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None)
