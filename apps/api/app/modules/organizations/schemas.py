"""Request/response models for the organization routes (contract m01-contracts.md §4)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

from app.modules.authorization.policy import OrgRole

# The stored format is ``^[a-z0-9][a-z0-9-]{1,62}$`` (ck_*_slug_format) and the API enforces
# exactly that: an upper-case slug is a 422 ``invalid_request``, never a misleading 409
# ``slug_taken`` (DC-018). ``slug_taken`` is reserved for real collisions.
SLUG_INPUT_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{1,62}$"
NAME_MAX_LENGTH: Final = 200

Slug = Annotated[str, StringConstraints(pattern=SLUG_INPUT_PATTERN)]
Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX_LENGTH)
]


EMAIL_PATTERN: Final = r"^[^@\s,;<>()\"]{1,64}@[^@\s,;<>()\"]+\.[^@\s,;<>()\"]+$"
Email = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=254, pattern=EMAIL_PATTERN),
    AfterValidator(str.lower),
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


class PreapprovalIn(_In):
    email: Email
    role: OrgRole


class Preapproval(BaseModel):
    id: uuid.UUID
    email: str
    role: OrgRole
    created_at: datetime


class PreapprovalPage(BaseModel):
    items: list[Preapproval]


class PreapprovalResult(BaseModel):
    """``member_added``: the person already had a verified account (membership now);
    ``pending``: ``preapproval`` is consumed at their first verified sign-in."""

    status: Literal["member_added", "pending"]
    member: Member | None = None
    preapproval: Preapproval | None = None
