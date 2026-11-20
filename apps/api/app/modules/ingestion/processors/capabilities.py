"""Optional-capability switches and pluggable engines of the media processors (M06.02.03-.05).

Settings come from the environment (names of m06-contracts.md section 5) and are independent of
``core.config``. Engines are adapters behind small protocols so tests (and later real backends)
plug in without touching the processors. A disabled capability or a missing engine is an explicit
``unsupported`` outcome (``ProcessingError`` with a capability code), never a crash.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Literal, Protocol

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FormatSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=True)

    OCR_ENABLED: bool = False
    OCR_ENGINE: Literal["none", "tesseract"] = "none"
    TRANSCRIBE_ENABLED: bool = False
    VIDEO_ENABLED: bool = False
    TRANSCRIBE_MAX_BYTES: int = Field(default=50 * 1024 * 1024, ge=1)
    TRANSCRIBE_MAX_SECONDS: int = Field(default=3600, ge=1)
    OCR_MAX_BYTES: int = Field(default=20 * 1024 * 1024, ge=1)


@lru_cache(maxsize=1)
def get_format_settings() -> FormatSettings:
    return FormatSettings()


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    engine: str
    version: str
    confidence: float | None = None  # mean confidence 0..1 when the engine reports one


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    start: float  # seconds
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    segments: list[TranscriptSegment]
    engine: str
    version: str
    duration: float | None = None
    language: str | None = None


class OcrEngine(Protocol):
    def recognize(self, data: bytes, content_type: str) -> OcrResult: ...


class TranscriptionEngine(Protocol):
    def transcribe(self, data: bytes, content_type: str) -> TranscriptResult: ...


class TesseractCliEngine:
    """Adapter around the ``tesseract`` binary (plain text out; no confidence)."""

    def __init__(self, binary: str) -> None:
        self._binary = binary

    def recognize(self, data: bytes, content_type: str) -> OcrResult:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, image bytes on stdin
            [self._binary, "stdin", "stdout"],
            input=data,
            capture_output=True,
            timeout=60,
            check=True,
        )
        return OcrResult(
            text=proc.stdout.decode("utf-8", errors="replace"), engine="tesseract", version="cli"
        )


@dataclass(slots=True)
class _Overrides:
    ocr: OcrEngine | None = None
    transcription: TranscriptionEngine | None = None
    settings: FormatSettings | None = None


_OVERRIDES: Final = _Overrides()


def set_engines_for_tests(
    *,
    ocr: OcrEngine | None = None,
    transcription: TranscriptionEngine | None = None,
    settings: FormatSettings | None = None,
) -> None:
    """Install fakes (and settings). Call ``reset_engines`` afterwards."""
    _OVERRIDES.ocr, _OVERRIDES.transcription, _OVERRIDES.settings = ocr, transcription, settings


def reset_engines() -> None:
    _OVERRIDES.ocr = _OVERRIDES.transcription = _OVERRIDES.settings = None


def current_settings() -> FormatSettings:
    return _OVERRIDES.settings or get_format_settings()


def get_ocr_engine() -> OcrEngine | None:
    """The engine to use, or ``None`` when OCR is disabled or no engine is available."""
    settings = current_settings()
    if not settings.OCR_ENABLED:
        return None
    if _OVERRIDES.ocr is not None:
        return _OVERRIDES.ocr
    if settings.OCR_ENGINE == "tesseract":
        binary = shutil.which("tesseract")
        return TesseractCliEngine(binary) if binary else None
    return None


def get_transcription_engine() -> TranscriptionEngine | None:
    settings = current_settings()
    if not settings.TRANSCRIBE_ENABLED:
        return None
    return _OVERRIDES.transcription


def capability_matrix() -> dict[str, bool]:
    return {
        "ocr": get_ocr_engine() is not None,
        "transcription": get_transcription_engine() is not None,
        "video": False,  # no video pipeline exists; VIDEO_ENABLED only changes nothing yet
    }
