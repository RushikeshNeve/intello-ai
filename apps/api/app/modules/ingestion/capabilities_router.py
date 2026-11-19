"""``GET /v1/capabilities``: what this deployment can ingest (M06.02.05). Static from settings."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.security import Principal, get_current_principal
from app.modules.ingestion.processors.capabilities import capability_matrix
from app.modules.ingestion.processors.registry import (
    AUDIO_CONTENT_TYPES,
    IMAGE_CONTENT_TYPES,
    SUPPORTED_CONTENT_TYPES,
    VIDEO_CONTENT_TYPES,
)

router = APIRouter(prefix="/v1", tags=["capabilities"])


class Capabilities(BaseModel):
    ocr: bool
    transcription: bool
    video: bool
    formats: list[str]


@router.get("/capabilities", response_model=Capabilities)
def get_capabilities(_: Annotated[Principal, Depends(get_current_principal)]) -> Capabilities:
    """``formats`` lists the content types that are ingested today (disabled ones are omitted)."""
    matrix = capability_matrix()
    gated = {
        *(IMAGE_CONTENT_TYPES if not matrix["ocr"] else ()),
        *(AUDIO_CONTENT_TYPES if not matrix["transcription"] else ()),
        *VIDEO_CONTENT_TYPES,
    }
    return Capabilities(**matrix, formats=sorted(SUPPORTED_CONTENT_TYPES - gated))
