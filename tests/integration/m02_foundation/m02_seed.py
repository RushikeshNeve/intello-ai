"""Seeding helpers for the M02 tables (run as the admin role, which bypasses RLS)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Connection, text

SHA_A = "a" * 64
SHA_B = "b" * 64


def insert_source(
    conn: Connection,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    created_by: uuid.UUID,
    kind: str = "upload",
) -> uuid.UUID:
    source_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO sources (id, org_id, workspace_id, kind, name, created_by)"
            " VALUES (:id, :o, :w, :k, 'Uploads', :u)"
        ),
        {"id": source_id, "o": org_id, "w": workspace_id, "k": kind, "u": created_by},
    )
    return source_id


def insert_document(
    conn: Connection,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    created_by: uuid.UUID,
    title: str = "Doc",
    document_id: uuid.UUID | None = None,
) -> uuid.UUID:
    doc_id = document_id or uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO documents (id, org_id, workspace_id, source_id, title, created_by)"
            " VALUES (:id, :o, :w, :s, :t, :u)"
        ),
        {"id": doc_id, "o": org_id, "w": workspace_id, "s": source_id, "t": title, "u": created_by},
    )
    return doc_id


def version_params(
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    created_by: uuid.UUID,
    version_number: int = 1,
    **overrides: Any,
) -> dict[str, Any]:
    version_id = overrides.pop("id", None) or uuid.uuid4()
    params: dict[str, Any] = {
        "id": version_id,
        "o": org_id,
        "w": workspace_id,
        "d": document_id,
        "n": version_number,
        "fn": "file.pdf",
        "ct": "application/pdf",
        "sz": 10,
        "sha": SHA_A,
        "k": f"org/{org_id}/doc/{document_id}/{version_id}/original",
        "u": created_by,
    }
    params.update(overrides)
    return params


INSERT_VERSION = text(
    "INSERT INTO document_versions (id, org_id, workspace_id, document_id, version_number,"
    " original_filename, content_type, size_bytes, sha256, object_key, created_by)"
    " VALUES (:id, :o, :w, :d, :n, :fn, :ct, :sz, :sha, :k, :u)"
)


def insert_version(
    conn: Connection,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
    created_by: uuid.UUID,
    version_number: int = 1,
    **overrides: Any,
) -> uuid.UUID:
    params = version_params(
        org_id=org_id,
        workspace_id=workspace_id,
        document_id=document_id,
        created_by=created_by,
        version_number=version_number,
        **overrides,
    )
    conn.execute(INSERT_VERSION, params)
    return uuid.UUID(str(params["id"]))


def insert_session(
    conn: Connection,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    created_by: uuid.UUID,
    state: str = "pending",
    **extra: Any,
) -> uuid.UUID:
    """Insert an upload session; ``extra`` may override failure_code / finalized_at / etc."""
    session_id = uuid.uuid4()
    version_id = extra.pop("version_id", None) or uuid.uuid4()
    document_id = extra.pop("document_id", None) or uuid.uuid4()
    failure_code = extra.pop("failure_code", "size_mismatch" if state == "failed" else None)
    finalized = extra.pop("finalized_at", "now()" if state == "finalized" else None)
    fragment = "now()" if finalized == "now()" else ":fin"
    params: dict[str, Any] = {
        "id": session_id,
        "o": org_id,
        "w": workspace_id,
        "d": document_id,
        "v": version_id,
        "u": created_by,
        "st": state,
        "fc": failure_code,
        "fin": None if finalized == "now()" else finalized,
        "key": f"org/{org_id}/doc/{document_id}/{version_id}/original",
        "sz": 10,
        "sha": SHA_A,
        "fn": "file.pdf",
        "ct": "application/pdf",
    }
    params.update(extra)
    conn.execute(
        text(
            "INSERT INTO upload_sessions (id, org_id, workspace_id, document_id, version_id,"
            " created_by, filename, content_type, size_bytes, sha256, object_key, state,"
            " failure_code, expires_at, finalized_at)"
            " VALUES (:id, :o, :w, :d, :v, :u, :fn, :ct, :sz, :sha, :key, :st, :fc,"
            f" now() + interval '1 hour', {fragment})"
        ),
        params,
    )
    return session_id


def seed_document(
    conn: Connection,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    created_by: uuid.UUID,
    source_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A document with one version set as latest. Returns (source_id, document_id, version_id)."""
    if source_id is None:
        source_id = insert_source(
            conn, org_id=org_id, workspace_id=workspace_id, created_by=created_by
        )
    doc = insert_document(
        conn, org_id=org_id, workspace_id=workspace_id, source_id=source_id, created_by=created_by
    )
    ver = insert_version(
        conn, org_id=org_id, workspace_id=workspace_id, document_id=doc, created_by=created_by
    )
    conn.execute(
        text("UPDATE documents SET latest_version_id = :v WHERE id = :d"), {"v": ver, "d": doc}
    )
    return source_id, doc, ver
