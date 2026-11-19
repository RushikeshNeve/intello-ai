"""Image OCR processor (M06.02.03): bytes -> text blocks with engine provenance.

Disabled capability or no engine: ``ProcessingError("ocr_unavailable")`` (job state
``unsupported``, no crash). The engine's name/version become the document's ``processor``
provenance (``ocr-<engine>``) and the mean confidence goes to ``stats.confidence``. Image-only PDFs
are NOT routed here yet (they stay ``no_text_extracted``).
"""

from __future__ import annotations

from app.modules.ingestion.chunking import clean_newlines_and_controls
from app.modules.ingestion.contracts import ExtractedDocument, ProcessingError, SourceInfo
from app.modules.ingestion.processors.base import DocumentBuilder, Limits, check_input_size
from app.modules.ingestion.processors.capabilities import current_settings, get_ocr_engine


def extract_image(
    data: bytes, content_type: str, *, source: SourceInfo, limits: Limits
) -> ExtractedDocument:
    engine = get_ocr_engine()
    if engine is None:
        raise ProcessingError("ocr_unavailable", detail="OCR is disabled or has no engine")
    check_input_size(data, limits)
    if len(data) > current_settings().OCR_MAX_BYTES:
        raise ProcessingError("too_large", detail="image larger than the OCR limit")
    try:
        result = engine.recognize(data, content_type)
    except ProcessingError:
        raise
    except Exception as exc:
        raise ProcessingError(
            "malformed_input", detail=f"ocr engine failed: {type(exc).__name__}"
        ) from exc
    builder = DocumentBuilder("image", source, f"ocr-{result.engine}"[:64], limits)
    for paragraph in clean_newlines_and_controls(result.text).split("\n\n"):
        builder.add("paragraph", paragraph)
    return builder.build(processor_version=result.version[:32], confidence=result.confidence)
