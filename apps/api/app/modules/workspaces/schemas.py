"""Request/response models for the workspace routes (contract m01-contracts.md §4)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.modules.authorization.policy import WorkspaceRole
from app.modules.organizations.schemas import Name, Slug


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceCreate(_In):
    slug: Slug
    name: Name


class WorkspaceUpdate(_In):
    name: Name | None = None


class Workspace(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    slug: str
    name: str
    created_at: datetime
    # The caller's effective role: org owners/admins are implicit workspace admins.
    role: WorkspaceRole


class WorkspacePage(BaseModel):
    items: list[Workspace]
    next_cursor: str | None


class WorkspaceMemberRoleIn(_In):
    role: WorkspaceRole


class WorkspaceMember(BaseModel):
    user_id: uuid.UUID
    email: str | None
    display_name: str | None
    role: WorkspaceRole
