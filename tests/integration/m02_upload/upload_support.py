"""Helpers for the upload API tests: a scene (org, users per role, workspaces) and API shortcuts."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from tenancy_support import Api, FakeVerifier, Response, User

from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.modules.storage.ports import ObjectStore


@dataclass
class StoreHolder:
    """The session-wide app holds one reference; each test swaps in a fresh store."""

    store: ObjectStore


@dataclass(frozen=True)
class Scene:
    """One org with every kind of caller, two workspaces, and an outsider with their own org."""

    org_id: uuid.UUID
    ws: uuid.UUID  # the workspace under test
    ws2: uuid.UUID  # another workspace of the same org
    owner: User  # org owner (implicit workspace admin)
    org_admin: User
    ws_admin: User
    editor: User
    viewer: User
    member: User  # org member without any workspace role
    outsider: User  # owner of another org, not a member of this one
    other_org_id: uuid.UUID
    other_ws: uuid.UUID  # a workspace of the outsider's org

    @property
    def base(self) -> str:
        return f"/v1/orgs/{self.org_id}/workspaces/{self.ws}"


def sha_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def body_for(
    data: bytes,
    *,
    filename: str = "report.txt",
    content_type: str = "text/plain",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "filename": filename,
        "content_type": content_type,
        "size_bytes": len(data),
        "sha256": sha_hex(data),
        **extra,
    }


class UploadApi(Api):
    def __init__(
        self,
        client: TestClient,
        verifier: FakeVerifier,
        admin_engine: Engine,
        store: ObjectStore,
    ) -> None:
        super().__init__(client, verifier, admin_engine)
        self.store = store  # the memory fake (tests simulate the browser) or the real S3 store

    # -- setup ---------------------------------------------------------------------------
    def scene(self) -> Scene:
        base = self.tenant()
        org = base.org_id
        ws = self.create_workspace(org, base.owner)
        ws2 = self.create_workspace(org, base.owner)
        ws_admin, editor, viewer = (
            self.user("wsadmin"),
            self.user("editor"),
            self.user("viewer"),
        )
        for user, role in ((ws_admin, "admin"), (editor, "editor"), (viewer, "viewer")):
            self.set_role(org, base.owner, user, "member")
            res = self.put(
                f"/v1/orgs/{org}/workspaces/{ws}/members/{user.id}", base.owner, {"role": role}
            )
            assert res.status == 200, res.body
        other_ws = self.create_workspace(base.other_org_id, base.outsider)
        return Scene(
            org_id=org,
            ws=ws,
            ws2=ws2,
            owner=base.owner,
            org_admin=base.admin,
            ws_admin=ws_admin,
            editor=editor,
            viewer=viewer,
            member=base.member,
            outsider=base.outsider,
            other_org_id=base.other_org_id,
            other_ws=other_ws,
        )

    def grant(
        self,
        scene: Scene,
        document_id: uuid.UUID,
        user: User,
        permission: str,
        *,
        expired: bool = False,
        revoked: bool = False,
    ) -> None:
        with self.admin_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO resource_grants (id, org_id, workspace_id, resource_type,"
                    " resource_id, grantee_type, grantee_id, permission, granted_by, expires_at,"
                    " revoked_at, revoked_by) VALUES (:id, :o, :w, 'document', :d, 'user', :g, :p,"
                    " :by, CASE WHEN :exp THEN now() - interval '1 hour' END,"
                    " CASE WHEN :rev THEN now() END, CASE WHEN :rev THEN :by END)"
                ),
                {
                    "id": uuid.uuid4(),
                    "o": scene.org_id,
                    "w": scene.ws,
                    "d": document_id,
                    "g": str(user.id),
                    "p": permission,
                    "by": scene.owner.id,
                    "exp": expired,
                    "rev": revoked,
                },
            )

    # -- calls ---------------------------------------------------------------------------
    def create(
        self, scene: Scene, user: User | None, body: Any, *, ws: uuid.UUID | None = None
    ) -> Response:
        return self.post(f"/v1/orgs/{scene.org_id}/workspaces/{ws or scene.ws}/uploads", user, body)

    def finalize(
        self,
        scene: Scene,
        user: User | None,
        upload_id: uuid.UUID | str,
        *,
        ws: uuid.UUID | None = None,
        org_id: uuid.UUID | None = None,
    ) -> Response:
        path = f"/v1/orgs/{org_id or scene.org_id}/workspaces/{ws or scene.ws}"
        return self.post(f"{path}/uploads/{upload_id}/finalize", user, None)

    def put_bytes(
        self, session: dict[str, Any], data: bytes, content_type: str = "text/plain"
    ) -> None:
        """The browser PUT: only succeeds if ``data`` is the body that was signed."""
        key = f"org/{session['_org']}/doc/{session['document_id']}/{session['version_id']}/original"
        assert isinstance(self.store, InMemoryObjectStore)
        self.store.simulate_upload(key, data, content_type)

    def session(
        self,
        scene: Scene,
        user: User,
        data: bytes,
        *,
        put: bool = True,
        **body_kw: Any,
    ) -> dict[str, Any]:
        """Create a session (asserting 201) and, by default, PUT ``data``. Returns the body."""
        body = body_for(data, **body_kw)
        res = self.create(scene, user, body)
        assert res.status == 201, res.body
        out: dict[str, Any] = dict(res.body)
        out["_org"] = str(scene.org_id)
        if put:
            self.put_bytes(out, data, body["content_type"])
        return out

    def uploaded(
        self, scene: Scene, user: User, data: bytes, **body_kw: Any
    ) -> tuple[dict[str, Any], Response]:
        """Create, PUT and finalize (asserting 201): returns the session and the finalize reply."""
        session = self.session(scene, user, data, **body_kw)
        res = self.finalize(scene, user, session["upload_id"])
        assert res.status == 201, res.body
        return session, res

    # -- database reads (admin, bypassing RLS) -----------------------------------------------
    def rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        with self.admin_engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql), params).mappings()]

    def execute(self, sql: str, **params: Any) -> None:
        with self.admin_engine.begin() as conn:
            conn.execute(text(sql), params)

    def session_row(self, upload_id: uuid.UUID | str) -> dict[str, Any]:
        found = self.rows("SELECT * FROM upload_sessions WHERE id = :i", i=upload_id)
        assert len(found) == 1
        return found[0]

    def versions(self, document_id: uuid.UUID | str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM document_versions WHERE document_id = :d ORDER BY version_number",
            d=document_id,
        )

    def count(self, table: str, where: str = "true", **params: Any) -> int:
        return int(self.scalar(f"SELECT count(*) FROM {table} WHERE {where}", **params))

    def expire_session(self, upload_id: uuid.UUID | str) -> None:
        """Move the session's lifetime into the past (``expires_at > created_at`` must hold)."""
        with self.admin_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE upload_sessions SET created_at = now() - interval '2 hours',"
                    " expires_at = now() - interval '1 hour' WHERE id = :i"
                ),
                {"i": upload_id},
            )
