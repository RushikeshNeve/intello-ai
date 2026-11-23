"""Default-deny tool registry and authorizer (M14.06, S6).

NO tool is registered today and nothing here is wired to a model: model output and retrieved text
never reach this module. A tool becomes callable only when ALL hold, checked in :func:`authorize`:

1. it is registered (``register_tool``), with scopes, a confirmation flag and an args schema;
2. the orchestrator explicitly enabled it for THIS run (``RunContext.enabled``): a registered tool
   is invisible to a run that did not enable it;
3. the user holds every scope the tool requires;
4. the run's call budget is not spent;
5. the arguments validate against the tool's schema (``extra=forbid``).

The result is ``allow``, ``deny`` or ``needs_confirmation``; every decision is audited with ids
only (user, workspace, run, tool name, outcome, reason code): never arguments or text.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ValidationError

logger = logging.getLogger("ukos.permissions_tools")

Outcome = Literal["allow", "deny", "needs_confirmation"]
AuditSink = Callable[[dict[str, str]], None]
DEFAULT_CALL_BUDGET: Final = 5


class ToolRegistrationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    scopes: frozenset[str]
    requires_confirmation: bool
    audit: bool
    args_schema: type[BaseModel]


@dataclass(frozen=True, slots=True)
class ToolUser:
    user_id: UUID
    scopes: frozenset[str]


@dataclass(slots=True)
class RunContext:
    """Per-run grant from the orchestrator; ``enabled`` is the ONLY way a tool is reachable."""

    run_id: UUID
    enabled: frozenset[str] = frozenset()
    max_calls: int = DEFAULT_CALL_BUDGET
    calls: int = field(default=0)


@dataclass(frozen=True, slots=True)
class Decision:
    outcome: Outcome
    reason: str

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"


class ToolRegistry:
    def __init__(self, audit_sink: AuditSink | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._sink: AuditSink = audit_sink or _log_sink

    def register_tool(
        self,
        name: str,
        scopes: frozenset[str] | set[str],
        requires_confirmation: bool,
        audit: bool = True,
        *,
        args_schema: type[BaseModel],
    ) -> ToolSpec:
        if not name or name in self._tools:
            raise ToolRegistrationError("tool name empty or already registered")
        if not scopes:
            raise ToolRegistrationError("a tool must require at least one scope")
        if not audit:
            # Contract: every tool call is audited; the flag exists for explicitness only.
            raise ToolRegistrationError("audit=False is not allowed")
        spec = ToolSpec(name, frozenset(scopes), requires_confirmation, audit, args_schema)
        self._tools[name] = spec
        return spec

    def names(self) -> list[str]:
        return sorted(self._tools)

    def authorize(
        self,
        user: ToolUser,
        workspace_id: UUID,
        run: RunContext,
        tool: str,
        args: Mapping[str, Any],
    ) -> Decision:
        decision = self._decide(user, run, tool, args)
        if decision.outcome == "allow":
            run.calls += 1
        self._sink(
            {
                "event": "tool.authorize",
                "user_id": str(user.user_id),
                "workspace_id": str(workspace_id),
                "run_id": str(run.run_id),
                "tool": tool if tool in self._tools else "unknown",
                "outcome": decision.outcome,
                "reason": decision.reason,
            }
        )
        return decision

    def _decide(
        self, user: ToolUser, run: RunContext, tool: str, args: Mapping[str, Any]
    ) -> Decision:
        spec = self._tools.get(tool)
        if spec is None:
            return Decision("deny", "unknown_tool")
        if tool not in run.enabled:
            return Decision("deny", "not_enabled_for_run")
        if not spec.scopes <= user.scopes:
            return Decision("deny", "missing_scope")
        if run.calls >= run.max_calls:
            return Decision("deny", "budget_exhausted")
        try:
            spec.args_schema.model_validate(dict(args), strict=True)
        except ValidationError:
            return Decision("deny", "invalid_arguments")
        if spec.requires_confirmation:
            return Decision("needs_confirmation", "confirmation_required")
        return Decision("allow", "ok")


def _log_sink(event: dict[str, str]) -> None:
    logger.info("tool authorize %s", event)


# The process-wide registry. Intentionally empty: see docs/architecture/m14-contracts.md.
REGISTRY: Final = ToolRegistry()
register_tool = REGISTRY.register_tool
authorize = REGISTRY.authorize
