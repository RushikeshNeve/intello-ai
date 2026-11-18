"""Shared data access and authorization for the document catalog (M02.02.02 - M02.02.04).

Private to ``api.py``, ``versions.py`` and ``deletion.py``. Every function takes a tenant session
opened with ``TenantRequest.transaction(org_id)``; queries also filter by the path org and
workspace explicitly, so an id from anywhere but the path is never trusted.

Design notes (contract m02-contracts.md §4, §5):

* Path resolution copies the M01 order: org (missing -> 404) -> org membership (non-member ->
  audited 404) -> workspace in that org (missing -> 404) -> document (missing/deleted -> 404, no
  matter who asks) -> ``decide()`` (no role and no grant -> audited 404, else audited 403).
* A user with **no workspace role** sees only documents on which they hold an *active user grant*
  (any permission implies read). A grant to a workspace role is irrelevant without the role.
* ``permissions {write, manage}`` come from the same ``decide()`` calls as the endpoints, so the UI
  hints can never disagree with enforcement.
* The list is one statement: the page, a correlated version count and the latest version joined
  in. Grants for the page are fetched with one more query, never per document.
* Keyset cursors carry the sort, the order and the last ``(sort key, id)``; a cursor made for a
  different sort/order is ``invalid_cursor``. ``id`` always follows the direction of the sort so the
  existing ``(workspace_id, <key>, id)`` indexes serve both directions with a plain index scan.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final

from sqlalchemy import ColumnElement, Select, and_, func, literal, or_, select, tuple_, update
from sqlalchemy.orm import Session, aliased

from app.api.deps import TenantRequest, not_found
from app.core.models import Document, DocumentVersion, ResourceGrant
from app.modules.audit import AuditEventType, record_event
from app.modules.authorization.grants import DOCUMENT, implied
from app.modules.authorization.policy import (
    Action,
    GrantPermission,
    OrgRole,
    SubjectContext,
    WorkspaceRole,
    decide,
)
from app.modules.documents.schemas import (
    Document as DocumentOut,
)
from app.modules.documents.schemas import (
    DocumentPermissions,
    DocumentSort,
    Retention,
    SortOrder,
)
from app.modules.documents.schemas import (
    Version as VersionOut,
)
from app.modules.organizations.service import OrgAccess, load_org_access
from app.modules.workspaces.service import WorkspaceRow, load_workspace

TARGET_DOCUMENT: Final = "document"
TARGET_WORKSPACE: Final = "workspace"
_ORG_MANAGERS: Final = frozenset({OrgRole.OWNER, OrgRole.ADMIN})
_NO_GRANTS: Final[frozenset[GrantPermission]] = frozenset()

_SORT_COLUMNS: Final = {
    "updated_at": Document.updated_at,
    "created_at": Document.created_at,
    "title": Document.title,
}
_LATEST = aliased(DocumentVersion, name="lv")


# --- request context ----------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceCtx:
    """The path org + workspace and the caller's roles in them."""

    org: OrgAccess  # ``org.subject.workspace_role`` is the caller's explicit workspace role
    workspace: WorkspaceRow

    @property
    def org_id(self) -> uuid.UUID:
        return self.org.org.id

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.workspace.id

    @property
    def user_id(self) -> uuid.UUID:
        return self.org.subject.user_id

    @property
    def role(self) -> WorkspaceRole | None:
        return self.org.subject.workspace_role

    @property
    def sees_every_document(self) -> bool:
        """Org owners/admins and every workspace member see every live document."""
        return self.org.role in _ORG_MANAGERS or self.role is not None

    def subject(self, grants: frozenset[GrantPermission] = _NO_GRANTS) -> SubjectContext:
        return replace(self.org.subject, grants=grants)

    def allowed(self, action: Action, grants: frozenset[GrantPermission] = _NO_GRANTS) -> bool:
        return decide(self.subject(grants), action).allowed

    def require(
        self,
        action: Action,
        grants: frozenset[GrantPermission] = _NO_GRANTS,
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        """``decide()``; raise the audited 404/403 (via ``AuthzDenied``) when it denies."""
        decision = decide(self.subject(grants), action)
        if not decision.allowed:
            raise self.org.denied(decision, action, target_type=target_type, target_id=target_id)


def resolve_workspace(
    tenant: TenantRequest,
    session: Session,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    action: Action,
) -> WorkspaceCtx:
    """Org -> membership -> workspace. Nothing about the workspace leaks to a non-member."""
    access = load_org_access(session, org_id, tenant.user_id)
    if access is None:
        raise not_found()
    if access.role is None:
        # Audited and concealed (404) before anything about the workspace is revealed.
        access.require(action, target_type=TARGET_WORKSPACE, target_id=str(workspace_id))
        raise not_found()  # unreachable: decide() always denies a non-member
    found = load_workspace(session, org_id, workspace_id, tenant.user_id)
    if found is None:
        raise not_found()
    workspace, role = found
    return WorkspaceCtx(
        org=replace(access, subject=replace(access.subject, workspace_role=role)),
        workspace=workspace,
    )


def audit(
    tenant: TenantRequest,
    session: Session,
    event_type: AuditEventType,
    *,
    ctx: WorkspaceCtx,
    document_id: uuid.UUID,
    metadata: dict[str, object],
) -> None:
    record_event(
        session,
        event_type=event_type,
        actor_user_id=tenant.user_id,
        org_id=ctx.org_id,
        target_type=TARGET_DOCUMENT,
        target_id=str(document_id),
        outcome="success",
        request_id=tenant.request_id,
        metadata={"workspace_id": str(ctx.workspace_id), **metadata},
    )


# --- grants -------------------------------------------------------------------------------


def _grant_is_active() -> ColumnElement[bool]:
    return and_(
        ResourceGrant.revoked_at.is_(None),
        or_(ResourceGrant.expires_at.is_(None), ResourceGrant.expires_at > func.now()),
    )


def grants_by_document(
    session: Session, ctx: WorkspaceCtx, document_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, frozenset[GrantPermission]]:
    """Active grants the caller holds on each document (closed under manage > write > read).

    One query for any number of documents. Org owners/admins are allowed everything by role, so
    nothing is loaded for them. Matches grants to the user and to the caller's explicit workspace
    role; expired and revoked grants never count.
    """
    if not document_ids or ctx.org.role in _ORG_MANAGERS:
        return {}
    g = ResourceGrant
    grantee: ColumnElement[bool] = and_(g.grantee_type == "user", g.grantee_id == str(ctx.user_id))
    if ctx.role is not None:
        grantee = or_(
            grantee, and_(g.grantee_type == "workspace_role", g.grantee_id == ctx.role.value)
        )
    rows = session.execute(
        select(g.resource_id, g.permission).where(
            g.org_id == ctx.org_id,
            g.workspace_id == ctx.workspace_id,
            g.resource_type == DOCUMENT,
            g.resource_id.in_(list(document_ids)),
            grantee,
            _grant_is_active(),
        )
    ).all()
    found: dict[uuid.UUID, set[str]] = {}
    for resource_id, permission in rows:
        found.setdefault(resource_id, set()).add(permission)
    return {rid: implied(perms) for rid, perms in found.items()}


def _visibility_filter(ctx: WorkspaceCtx) -> ColumnElement[bool]:
    g = ResourceGrant
    return (
        select(literal(1))
        .where(
            g.org_id == Document.org_id,
            g.resource_type == DOCUMENT,
            g.resource_id == Document.id,
            g.grantee_type == "user",
            g.grantee_id == str(ctx.user_id),
            _grant_is_active(),
        )
        .correlate(Document)
        .exists()
    )


def has_visible_document(session: Session, ctx: WorkspaceCtx) -> bool:
    """True when the caller holds an active user grant on any live document in the workspace."""
    stmt = select(literal(1)).where(
        Document.org_id == ctx.org_id,
        Document.workspace_id == ctx.workspace_id,
        Document.deleted_at.is_(None),
        _visibility_filter(ctx),
    )
    return session.execute(stmt.limit(1)).first() is not None


# --- cursors ------------------------------------------------------------------------------


@dataclass(frozen=True)
class PageCursor:
    """Opaque keyset position: ``scope`` (what it was made for), the last sort key and id."""

    scope: str
    key: str
    id: uuid.UUID

    def encode(self) -> str:
        raw = json.dumps([self.scope, self.key, str(self.id)], separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str, scope: str) -> PageCursor:
        """Raises ``ValueError`` for garbage or for a cursor made for another ``scope``."""
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
            parsed = json.loads(raw)
            if not (
                isinstance(parsed, list)
                and len(parsed) == 3
                and all(isinstance(part, str) for part in parsed)
            ):
                raise ValueError("shape")
            cursor_scope, key, ident = parsed
            if cursor_scope != scope:
                raise ValueError("scope")
            return cls(scope=scope, key=key, id=uuid.UUID(ident))
        except (ValueError, UnicodeDecodeError, binascii.Error, RecursionError) as exc:
            raise ValueError("invalid cursor") from exc


def document_scope(sort: DocumentSort, order: SortOrder) -> str:
    return f"documents:{sort}:{order}"


def _sort_value(sort: DocumentSort, key: str) -> str | datetime:
    """The cursor key as a SQL value; ``ValueError`` when it is not valid for the sort."""
    if sort == "title":
        return key
    stamp = datetime.fromisoformat(key)
    if stamp.tzinfo is None:
        raise ValueError("naive timestamp")
    return stamp


def _like_escape(text_: str) -> str:
    """Escape backslash, ``%`` and ``_`` so user text matches literally in ``LIKE ... ESCAPE``."""
    return text_.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- queries ------------------------------------------------------------------------------


def _version_out(m: Any, prefix: str, latest_version_id: uuid.UUID | None) -> VersionOut:
    version_id = m[f"{prefix}id"]
    return VersionOut(
        id=version_id,
        document_id=m[f"{prefix}document_id"],
        version_number=m[f"{prefix}version_number"],
        original_filename=m[f"{prefix}original_filename"],
        content_type=m[f"{prefix}content_type"],
        size_bytes=m[f"{prefix}size_bytes"],
        sha256=m[f"{prefix}sha256"],
        created_by=m[f"{prefix}created_by"],
        created_at=m[f"{prefix}created_at"],
        retention=Retention(
            status="current" if version_id == latest_version_id else "superseded",
            purge_after=None,
        ),
    )


@dataclass(frozen=True)
class DocumentRow:
    id: uuid.UUID
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    version_count: int
    latest_version: VersionOut | None


def _document_select(ctx: WorkspaceCtx) -> Select[Any]:
    count = (
        select(func.count())
        .select_from(DocumentVersion)
        .where(DocumentVersion.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )
    return (
        select(
            Document.id,
            Document.org_id,
            Document.workspace_id,
            Document.title,
            Document.created_by,
            Document.created_at,
            Document.updated_at,
            Document.latest_version_id,
            count.label("version_count"),
            _LATEST.id.label("lv_id"),
            _LATEST.document_id.label("lv_document_id"),
            _LATEST.version_number.label("lv_version_number"),
            _LATEST.original_filename.label("lv_original_filename"),
            _LATEST.content_type.label("lv_content_type"),
            _LATEST.size_bytes.label("lv_size_bytes"),
            _LATEST.sha256.label("lv_sha256"),
            _LATEST.created_by.label("lv_created_by"),
            _LATEST.created_at.label("lv_created_at"),
        )
        .select_from(Document)
        .outerjoin(
            _LATEST,
            and_(_LATEST.id == Document.latest_version_id, _LATEST.document_id == Document.id),
        )
        .where(
            Document.org_id == ctx.org_id,
            Document.workspace_id == ctx.workspace_id,
            Document.deleted_at.is_(None),
        )
    )


def _document_row(row: Any) -> DocumentRow:
    m = row._mapping
    latest = None if m["lv_id"] is None else _version_out(m, "lv_", m["latest_version_id"])
    return DocumentRow(
        id=m["id"],
        org_id=m["org_id"],
        workspace_id=m["workspace_id"],
        title=m["title"],
        created_by=m["created_by"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
        version_count=m["version_count"],
        latest_version=latest,
    )


def fetch_document(
    session: Session, ctx: WorkspaceCtx, document_id: uuid.UUID
) -> DocumentRow | None:
    """A live document of the path workspace; ``None`` when missing, deleted or elsewhere."""
    row = session.execute(_document_select(ctx).where(Document.id == document_id)).first()
    return None if row is None else _document_row(row)


def lock_document(session: Session, ctx: WorkspaceCtx, document_id: uuid.UUID) -> str | None:
    """``SELECT ... FOR UPDATE`` of a live document; returns its current title (``None`` = gone)."""
    title: str | None = session.execute(
        select(Document.title)
        .where(
            Document.id == document_id,
            Document.org_id == ctx.org_id,
            Document.workspace_id == ctx.workspace_id,
            Document.deleted_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    return title


def authorize_document(
    session: Session, ctx: WorkspaceCtx, document_id: uuid.UUID, action: Action
) -> tuple[DocumentRow, frozenset[GrantPermission]]:
    """Load a live document and require ``action`` on it (grants considered)."""
    row = fetch_document(session, ctx, document_id)
    if row is None:
        raise not_found()
    grants = grants_by_document(session, ctx, [document_id]).get(document_id, _NO_GRANTS)
    ctx.require(action, grants, target_type=TARGET_DOCUMENT, target_id=str(document_id))
    return row, grants


def document_out(
    ctx: WorkspaceCtx, row: DocumentRow, grants: frozenset[GrantPermission]
) -> DocumentOut:
    return DocumentOut(
        id=row.id,
        org_id=row.org_id,
        workspace_id=row.workspace_id,
        title=row.title,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version_count=row.version_count,
        latest_version=row.latest_version,
        permissions=DocumentPermissions(
            write=ctx.allowed(Action.DOCUMENT_WRITE, grants),
            manage=ctx.allowed(Action.DOCUMENT_MANAGE, grants),
        ),
    )


@dataclass(frozen=True)
class ListFilters:
    q: str | None
    content_type: str | None
    sort: DocumentSort
    order: SortOrder
    limit: int
    after: PageCursor | None


def list_documents(
    session: Session, ctx: WorkspaceCtx, filters: ListFilters
) -> tuple[list[DocumentRow], str | None]:
    """One page of the caller's visible live documents, plus the next cursor."""
    stmt = _document_select(ctx)
    if not ctx.sees_every_document:
        stmt = stmt.where(_visibility_filter(ctx))
    if filters.q:
        pattern = f"%{_like_escape(filters.q)}%"
        stmt = stmt.where(
            or_(
                Document.title.ilike(pattern, escape="\\"),
                _LATEST.original_filename.ilike(pattern, escape="\\"),
            )
        )
    if filters.content_type:
        if filters.content_type.endswith("/"):
            stmt = stmt.where(
                _LATEST.content_type.like(f"{_like_escape(filters.content_type)}%", escape="\\")
            )
        else:
            stmt = stmt.where(_LATEST.content_type == filters.content_type)

    column = _SORT_COLUMNS[filters.sort]
    descending = filters.order == "desc"
    if filters.after is not None:
        position = tuple_(column, Document.id)
        bound = tuple_(_sort_value(filters.sort, filters.after.key), filters.after.id)
        stmt = stmt.where(position < bound if descending else position > bound)
    stmt = stmt.order_by(
        column.desc() if descending else column.asc(),
        Document.id.desc() if descending else Document.id.asc(),
    ).limit(filters.limit + 1)

    rows = [_document_row(r) for r in session.execute(stmt).all()]
    page = rows[: filters.limit]
    next_cursor = None
    if len(rows) > filters.limit and page:
        last = page[-1]
        key = last.title if filters.sort == "title" else getattr(last, filters.sort).isoformat()
        next_cursor = PageCursor(document_scope(filters.sort, filters.order), key, last.id).encode()
    return page, next_cursor


def validate_cursor_key(sort: DocumentSort, cursor: PageCursor) -> None:
    """``ValueError`` when the cursor's key is not valid for ``sort`` (checked before any query)."""
    _sort_value(sort, cursor.key)


# --- versions -----------------------------------------------------------------------------

VERSION_SCOPE: Final = "versions:desc"


@dataclass(frozen=True)
class VersionRow:
    version: VersionOut
    object_key: str
    purged: bool


def _version_row(m: Any, latest_version_id: uuid.UUID | None) -> VersionRow:
    return VersionRow(
        version=_version_out(m, "", latest_version_id),
        object_key=m["object_key"],
        purged=m["purged_at"] is not None,
    )


_VERSION_COLUMNS = (
    DocumentVersion.id,
    DocumentVersion.document_id,
    DocumentVersion.version_number,
    DocumentVersion.original_filename,
    DocumentVersion.content_type,
    DocumentVersion.size_bytes,
    DocumentVersion.sha256,
    DocumentVersion.created_by,
    DocumentVersion.created_at,
    DocumentVersion.object_key,
    DocumentVersion.purged_at,
)


def latest_version_id_of(
    session: Session, ctx: WorkspaceCtx, document_id: uuid.UUID
) -> uuid.UUID | None:
    value: uuid.UUID | None = session.execute(
        select(Document.latest_version_id).where(
            Document.id == document_id,
            Document.org_id == ctx.org_id,
            Document.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    return value


def list_versions(
    session: Session,
    ctx: WorkspaceCtx,
    document_id: uuid.UUID,
    latest_version_id: uuid.UUID | None,
    *,
    limit: int,
    after: PageCursor | None,
) -> tuple[list[VersionOut], str | None]:
    """Versions of a document, newest first (``version_number`` is unique per document)."""
    stmt = select(*_VERSION_COLUMNS).where(
        DocumentVersion.document_id == document_id,
        DocumentVersion.org_id == ctx.org_id,
        DocumentVersion.workspace_id == ctx.workspace_id,
    )
    if after is not None:
        stmt = stmt.where(DocumentVersion.version_number < int(after.key))
    stmt = stmt.order_by(DocumentVersion.version_number.desc()).limit(limit + 1)
    rows = [_version_row(r._mapping, latest_version_id) for r in session.execute(stmt).all()]
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1].version
        next_cursor = PageCursor(VERSION_SCOPE, str(last.version_number), last.id).encode()
    return [r.version for r in page], next_cursor


def validate_version_cursor(cursor: PageCursor) -> None:
    int(cursor.key)


def fetch_version(
    session: Session,
    ctx: WorkspaceCtx,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    latest_version_id: uuid.UUID | None,
) -> VersionRow | None:
    """A version of *this* document (a version id of another document is ``None``)."""
    row = session.execute(
        select(*_VERSION_COLUMNS).where(
            DocumentVersion.id == version_id,
            DocumentVersion.document_id == document_id,
            DocumentVersion.org_id == ctx.org_id,
            DocumentVersion.workspace_id == ctx.workspace_id,
        )
    ).first()
    return None if row is None else _version_row(row._mapping, latest_version_id)


# --- writes -------------------------------------------------------------------------------


def rename_document(
    session: Session, ctx: WorkspaceCtx, document_id: uuid.UUID, title: str
) -> None:
    session.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.org_id == ctx.org_id,
            Document.workspace_id == ctx.workspace_id,
            Document.deleted_at.is_(None),
        )
        .values(title=title, updated_at=func.now())
        .execution_options(synchronize_session=False)
    )


def soft_delete_document(
    session: Session, ctx: WorkspaceCtx, document_id: uuid.UUID, deleted_by: uuid.UUID
) -> datetime | None:
    """Set ``deleted_at``/``deleted_by``; returns ``deleted_at`` or ``None`` if already deleted."""
    deleted_at: datetime | None = session.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.org_id == ctx.org_id,
            Document.workspace_id == ctx.workspace_id,
            Document.deleted_at.is_(None),
        )
        .values(deleted_at=func.now(), deleted_by=deleted_by)
        .returning(Document.deleted_at)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    return deleted_at
