"""Agent and tool permission layer (M14.06): default-deny, empty registry."""

from app.modules.permissions_tools.registry import (
    REGISTRY,
    Decision,
    RunContext,
    ToolRegistrationError,
    ToolRegistry,
    ToolSpec,
    ToolUser,
    authorize,
    register_tool,
)

__all__ = [
    "REGISTRY",
    "Decision",
    "RunContext",
    "ToolRegistrationError",
    "ToolRegistry",
    "ToolSpec",
    "ToolUser",
    "authorize",
    "register_tool",
]
