"""``GET /v1/me``: the authenticated caller (contract docs/architecture/m01-contracts.md §4)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.security import Principal, get_current_principal

router = APIRouter(prefix="/v1", tags=["identity"])


class MeResponse(BaseModel):
    id: uuid.UUID
    email: str | None
    display_name: str | None


@router.get("/me", response_model=MeResponse)
async def me(principal: Annotated[Principal, Depends(get_current_principal)]) -> MeResponse:
    return MeResponse(
        id=principal.user_id, email=principal.email, display_name=principal.display_name
    )
