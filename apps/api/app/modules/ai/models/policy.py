"""Model policy (contract section 5): who may use which provider.

``ModelPolicy`` is the per-org row (``ai_policies``); the DB-backed loader lives elsewhere and
implements ``PolicySource``. Denials are typed, user-safe ``PolicyDenied`` errors with a stable
``reason`` code; the text never contains a secret, a URL or a model id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

KNOWN_PROVIDERS = frozenset({"local", "openai"})

DENIAL_MESSAGES = {
    "external_disabled": "External AI providers are disabled on this server.",
    "org_policy_denies_external": "Your organization's AI policy does not allow external providers.",  # noqa: E501
    "provider_not_allowed": "The requested AI provider is not allowed by your organization's policy.",  # noqa: E501
    "missing_credentials": "The requested AI provider is not configured.",
    "no_allowed_provider": "No AI provider is available under your organization's policy.",
    "endpoint_not_local": "The configured AI endpoint is not local and external providers are not permitted.",  # noqa: E501
    "unknown_provider": "The requested AI provider is not supported.",
    "hosted_not_allowed": "Hosted AI providers have not been enabled for your organization by an owner.",  # noqa: E501
    "policy_unavailable": "The organization's AI policy could not be read.",
}


class PolicyDenied(Exception):
    """No allowed chat model. ``reason`` is a stable code; ``str(exc)`` is safe to show a user."""

    transient = False

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in DENIAL_MESSAGES else "no_allowed_provider"
        super().__init__(DENIAL_MESSAGES[self.reason])


@dataclass(frozen=True)
class ModelPolicy:
    allow_external: bool = False
    allowed_providers: frozenset[str] = field(default_factory=lambda: frozenset({"local"}))
    hosted_models_allowed: bool = False  # owner opt-in (M14.05)

    @classmethod
    def local_only(cls) -> ModelPolicy:
        return cls(allow_external=False, allowed_providers=frozenset({"local"}))


class PolicySource(Protocol):
    def get_policy(self, org_id: UUID) -> ModelPolicy: ...


class InMemoryPolicySource:
    """Dict-backed source for tests and local wiring; unknown orgs get the default (local only)."""

    def __init__(
        self, policies: dict[UUID, ModelPolicy] | None = None, *, default: ModelPolicy | None = None
    ) -> None:
        self._policies = dict(policies or {})
        self._default = default if default is not None else ModelPolicy.local_only()

    def set_policy(self, org_id: UUID, policy: ModelPolicy) -> None:
        self._policies[org_id] = policy

    def get_policy(self, org_id: UUID) -> ModelPolicy:
        return self._policies.get(org_id, self._default)
