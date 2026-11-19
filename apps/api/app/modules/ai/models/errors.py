"""Typed chat-model errors. Every error exposes ``.transient`` (retrying later may help).

Messages are fixed, short and safe: never prompt/answer text, the API key, a response body or a URL.
"""

from __future__ import annotations


class ChatModelError(Exception):
    transient: bool = False


class ChatModelUnavailable(ChatModelError):
    """Unreachable, timing out, overloaded (429) or failing (5xx) after the retry budget."""

    transient = True

    def __init__(self, reason: str, *, attempts: int = 1) -> None:
        super().__init__(f"chat model unavailable after {attempts} attempt(s): {reason}")
        self.reason = reason
        self.attempts = attempts


class ChatRequestRejected(ChatModelError):
    """The server refused the request (4xx other than 429). Never retried."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"chat model rejected the request (HTTP {status_code})")
        self.status_code = status_code


class ChatResponseInvalid(ChatModelError):
    """The server answered but the body/stream is not a valid chat completion."""


class ChatStreamInterrupted(ChatModelError):
    """The stream failed or ended without completing. After the first token it is never retried
    here (it would duplicate text); the caller decides (usually: fail the run)."""

    transient = True

    def __init__(self, reason: str) -> None:
        super().__init__(f"chat stream interrupted: {reason}")
        self.reason = reason


class ChatConfigurationError(ChatModelError):
    """Settings are unusable or unsafe (bad URL, non-local endpoint without permission)."""
