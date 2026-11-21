"""Versioned prompt registry (M12.01). Use `resolve`; safety scaffolding stays in code."""

from app.modules.prompts.registry import KINDS, Resolved, default_body, resolve, sha256_of
from app.modules.prompts.scaffold import (
    BodyRejected,
    chat_system,
    graph_system,
    plan_system,
    validate_body,
)

__all__ = [
    "KINDS",
    "BodyRejected",
    "Resolved",
    "chat_system",
    "default_body",
    "graph_system",
    "plan_system",
    "resolve",
    "sha256_of",
    "validate_body",
]
