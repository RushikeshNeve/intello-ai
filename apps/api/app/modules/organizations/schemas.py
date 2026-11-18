"""Request/response models for the organization routes (contract m01-contracts.md §4)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.modules.authorization.policy import OrgRole

# The stored format is ``^[a-z0-9][a-z0-9-]{1,62}$`` (ck_*_slug_format). Upper-case letters are
# accepted here so that a case variant of an existing slug reaches the database and is answered
# 409 ``slug_taken`` (contract §2); every other malformed slug is a 422.
SLUG_INPUT_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9-]{1,62}$"
NAME_MAX_LENGTH: Final = 200

Slug = Annotated[str, StringConstraints(pattern=SLUG_INPUT_PATTERN)]
Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX_LENGTH)
]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrgCreate(_In):
    slug: Slug
    name: Name


class OrgUpdate(_In):
    name: Name | None = None


class OrgSummary(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    role: OrgRole


class Org(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    created_at: datetime
    role: OrgRole


class OrgPage(BaseModel):
    items: list[OrgSummary]
    next_cursor: str | None


class MemberRoleIn(_In):
    role: OrgRole


class Member(BaseModel):
    user_id: uuid.UUID
    email: str | None
    display_name: str | None
    role: OrgRole


class MemberPage(BaseModel):
    items: list[Member]
    next_cursor: str | None
