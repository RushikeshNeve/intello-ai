"""Seeding and request helpers for the catalog tests. Everything is seeded as the admin role."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, text
from tenancy_support import Api, Response, Tenant, User

SHA = "a" * 64
PRESIGN_TTL = 120  # S3_PRESIGN_TTL_SECONDS the tests run the API with


@dataclass(frozen=True)
class Seeded:
    id: uuid.UUID
    versions: list[uuid.UUID]  # oldest first; the last one is the latest


@dataclass
class World:
    """One org (owner/admin/member + ws roles), one workspace, an outsider and a foreign org."""

    api: Api
    t: Tenant
    ws: uuid.UUID
    ws_admin: User
    editor: User
    viewer: User
    plain: User  # org member without a workspace role (== t.member)
    other_plain: User  # a second org member without a workspace role
    foreign: Tenant  # another org with its own workspace
    foreign_ws: uuid.UUID
    _source: uuid.UUID | None = field(default=None, repr=False)
    _foreign_source: uuid.UUID | None = field(default=None, repr=False)

    @property
    def org_id(self) -> uuid.UUID:
        return self.t.org_id

    def path(self, sub: str = "") -> str:
        return f"/v1/orgs/{self.t.org_id}/workspaces/{self.ws}{sub}"

    def doc_path(self, document_id: uuid.UUID | str, sub: str = "") -> str:
        return self.path(f"/documents/{document_id}{sub}")

    @property
    def engine(self) -> Engine:
        return self.api.admin_engine

    @property
    def everyone(self) -> dict[str, User]:
        return {
            "owner": self.t.owner,
            "admin": self.t.admin,
            "ws_admin": self.ws_admin,
            "editor": self.editor,
            "viewer": self.viewer,
        }

    def source(self) -> uuid.UUID:
        if self._source is None:
            self._source = ensure_source(self.engine, self.org_id, self.ws, self.t.owner.id)
        return self._source

    def seed(self, **kw: Any) -> Seeded:
        return seed_document(
            self.engine,
            org_id=self.org_id,
            workspace_id=self.ws,
            source_id=self.source(),
            created_by=self.t.owner.id,
            **kw,
        )

    def seed_foreign(self, **kw: Any) -> Seeded:
        if self._foreign_source is None:
            self._foreign_source = ensure_source(
                self.engine, self.foreign.org_id, self.foreign_ws, self.foreign.owner.id
            )
        return seed_document(
            self.engine,
            org_id=self.foreign.org_id,
            workspace_id=self.foreign_ws,
            source_id=self._foreign_source,
            created_by=self.foreign.owner.id,
            **kw,
        )

    def grant(
        self,
        document_id: uuid.UUID,
        user: User,
        permission: str,
        *,
        expires_in: str | None = None,
        revoked: bool = False,
        grantee_type: str = "user",
        grantee_id: str | None = None,
    ) -> None:
        """An active grant; ``expires_in`` (SQL interval) or ``revoked`` make it inactive."""
        expires = f"now() + interval '{expires_in}'" if expires_in else "NULL"
        revoked_at = "now()" if revoked else "NULL"
        revoked_by = ":by" if revoked else "NULL"
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO resource_grants (id, org_id, workspace_id, resource_type,"
                    " resource_id, grantee_type, grantee_id, permission, granted_by, created_at,"
                    f" expires_at, revoked_at, revoked_by) VALUES (:id, :o, :w, 'document', :d,"
                    f" :gt, :gi, :p, :by, now() - interval '1 day', {expires}, {revoked_at},"
                    f" {revoked_by})"
                ),
                {
                    "id": uuid.uuid4(),
                    "o": self.org_id,
                    "w": self.ws,
                    "d": document_id,
                    "gt": grantee_type,
                    "gi": grantee_id or str(user.id),
                    "p": permission,
                    "by": self.t.owner.id,
                },
            )

    def row(self, sql: str, **params: Any) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).first()

    def titles(self, res: Response) -> list[str]:
        assert res.status == 200, res.body
        return [item["title"] for item in res.body["items"]]


def ensure_source(
    engine: Engine, org_id: uuid.UUID, workspace_id: uuid.UUID, created_by: uuid.UUID
) -> uuid.UUID:
    source_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sources (id, org_id, workspace_id, kind, name, created_by)"
                " VALUES (:id, :o, :w, 'upload', 'Uploads', :u)"
            ),
            {"id": source_id, "o": org_id, "w": workspace_id, "u": created_by},
        )
    return source_id


def seed_document(
    engine: Engine,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    created_by: uuid.UUID,
    title: str = "Doc",
    filename: str = "file.pdf",
    content_type: str = "application/pdf",
    versions: int = 1,
    created_at: datetime | str | None = None,
    updated_at: datetime | str | None = None,
    deleted: bool = False,
    document_id: uuid.UUID | None = None,
) -> Seeded:
    """A document with ``versions`` versions (the last is latest). Only the latest uses
    ``filename``/``content_type``; older ones are ``v<n>-<filename>`` with the same type."""
    doc_id = document_id or uuid.uuid4()
    created = created_at if created_at is not None else datetime.now().astimezone()
    updated = updated_at if updated_at is not None else created
    version_ids: list[uuid.UUID] = []
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO documents (id, org_id, workspace_id, source_id, title, created_by,"
                " created_at, updated_at, deleted_at, deleted_by) VALUES (:id, :o, :w, :s, :t,"
                " :u, :c, :up, CASE WHEN :del THEN now() END, CASE WHEN :del THEN :u END)"
            ),
            {
                "id": doc_id,
                "o": org_id,
                "w": workspace_id,
                "s": source_id,
                "t": title,
                "u": created_by,
                "c": created,
                "up": updated,
                "del": deleted,
            },
        )
        for number in range(1, versions + 1):
            version_id = uuid.uuid4()
            version_ids.append(version_id)
            name = filename if number == versions else f"v{number}-{filename}"
            conn.execute(
                text(
                    "INSERT INTO document_versions (id, org_id, workspace_id, document_id,"
                    " version_number, original_filename, content_type, size_bytes, sha256,"
                    " object_key, created_by, created_at) VALUES (:id, :o, :w, :d, :n, :fn, :ct,"
                    " :sz, :sha, :k, :u, :c)"
                ),
                {
                    "id": version_id,
                    "o": org_id,
                    "w": workspace_id,
                    "d": doc_id,
                    "n": number,
                    "fn": name,
                    "ct": content_type,
                    "sz": 100 * number,
                    "sha": SHA,
                    "k": f"org/{org_id}/doc/{doc_id}/{version_id}/original",
                    "u": created_by,
                    "c": created,
                },
            )
        conn.execute(
            text("UPDATE documents SET latest_version_id = :v WHERE id = :d"),
            {"v": version_ids[-1], "d": doc_id},
        )
    return Seeded(doc_id, version_ids)


def build_world(api: Api) -> World:
    t = api.tenant()
    ws = api.create_workspace(t.org_id, t.owner)
    people: dict[str, User] = {}
    for role in ("admin", "editor", "viewer"):
        user = api.user(f"ws{role}")
        api.set_role(t.org_id, t.owner, user, "member")
        res = api.put(
            f"/v1/orgs/{t.org_id}/workspaces/{ws}/members/{user.id}", t.owner, {"role": role}
        )
        assert res.status == 200, res.body
        people[role] = user
    other_plain = api.user("plain2")
    api.set_role(t.org_id, t.owner, other_plain, "member")
    foreign = api.tenant()
    foreign_ws = api.create_workspace(foreign.org_id, foreign.owner)
    return World(
        api=api,
        t=t,
        ws=ws,
        ws_admin=people["admin"],
        editor=people["editor"],
        viewer=people["viewer"],
        plain=t.member,
        other_plain=other_plain,
        foreign=foreign,
        foreign_ws=foreign_ws,
    )
