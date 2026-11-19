"""Audio transcript processor (M06.02.04) and the video capability stub (M06.02.05).

Segments from the engine are merged into blocks of at most ``_WINDOW_SECONDS`` / ``_WINDOW_CHARS``;
each block has a ``time`` locator (``time_start``/``time_end`` seconds). Limits: input bytes
(``TRANSCRIBE_MAX_BYTES``) before the engine runs, media duration (``TRANSCRIBE_MAX_SECONDS``)
after. Disabled or no engine: ``transcription_unavailable``. Video is never processed:
``video_unavailable`` (the ``VIDEO_ENABLED`` flag has no pipeline behind it yet).
"""

from __future__ import annotations

from typing import Final

from app.modules.ingestion.contracts import (
    MAX_MEDIA_SECONDS,
    ExtractedDocument,
    ProcessingError,
    SourceInfo,
)
from app.modules.ingestion.processors.base import DocumentBuilder, Limits, check_input_size
from app.modules.ingestion.processors.capabilities import (
    current_settings,
    get_transcription_engine,
)

_WINDOW_SECONDS: Final = 30.0
_WINDOW_CHARS: Final = 1200


def extract_audio(
    data: bytes, content_type: str, *, source: SourceInfo, limits: Limits
) -> ExtractedDocument:
    engine = get_transcription_engine()
    if engine is None:
        raise ProcessingError(
            "transcription_unavailable", detail="transcription is disabled or has no engine"
        )
    settings = current_settings()
    check_input_size(data, limits)
    if len(data) > settings.TRANSCRIBE_MAX_BYTES:
        raise ProcessingError("too_large", detail="audio larger than the transcription limit")
    try:
        result = engine.transcribe(data, content_type)
    except ProcessingError:
        raise
    except Exception as exc:
        raise ProcessingError(
            "malformed_input", detail=f"transcription engine failed: {type(exc).__name__}"
        ) from exc
    duration = result.duration or max((s.end for s in result.segments), default=0.0)
    if duration > min(settings.TRANSCRIBE_MAX_SECONDS, MAX_MEDIA_SECONDS):
        raise ProcessingError("too_large", detail="audio longer than the transcription limit")

    builder = DocumentBuilder("audio", source, f"transcript-{result.engine}"[:64], limits)
    pending: list[str] = []
    start = end = 0.0

    def flush() -> None:
        if pending:
            builder.add(
                "paragraph",
                " ".join(pending),
                extra={"time_start": start, "time_end": max(end, start)},
            )
            pending.clear()

    for seg in sorted(result.segments, key=lambda s: (s.start, s.end)):
        text = " ".join(seg.text.split())
        if not text:
            continue
        if pending and (
            seg.end - start > _WINDOW_SECONDS or sum(map(len, pending)) > _WINDOW_CHARS
        ):
            flush()
        if not pending:
            start = max(seg.start, 0.0)
            end = start
        end = max(seg.end, end)
        pending.append(text)
    flush()
    return builder.build(
        processor_version=result.version[:32],
        language=result.language,
        duration_seconds=float(duration),
    )


def extract_video(
    data: bytes, content_type: str, *, source: SourceInfo, limits: Limits
) -> ExtractedDocument:
    raise ProcessingError("video_unavailable", detail="video processing is not supported")
