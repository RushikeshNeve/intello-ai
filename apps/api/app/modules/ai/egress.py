"""Central outbound-network control (M14.05, S5).

Every outbound HTTP client of the API/worker (model providers, embeddings, connectors, URL capture,
OIDC/JWKS, RushDB, OTLP exporter, future webhooks) calls :func:`check` (usually through
:func:`request_hook` on its ``httpx.Client``). Rules:

* ``EGRESS_ALLOWED_HOSTS`` (comma separated; ``host`` or ``*.suffix`` patterns). EMPTY = allow all
  with a warning (dev). In production the list is enforced and must include internal hosts too
  (database-adjacent services, the OIDC issuer, the OTLP collector, the model endpoint).
* Purposes in :data:`USER_URL_PURPOSES` carry a user-supplied URL: they are exempt from the
  allowlist (the public web cannot be enumerated) but the host is resolved and every address must
  be a global unicast address (private, loopback, link-local and metadata ranges are refused;
  resolving happens here and again at connect time by the caller's pinning transport).
* Logs record host + purpose + decision only: never a path, query, header, body or key.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import logging
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

from app.core.config import get_settings

logger = logging.getLogger("ukos.egress")

EGRESS_BLOCKED: Final = "egress_blocked"
USER_URL_PURPOSES: Final = frozenset({"url_capture", "webhook_user"})

Resolver = Callable[[str], Sequence[str]]


class EgressDenied(Exception):
    """The outbound call is refused. ``reason`` is a stable code; the text never echoes a URL."""

    def __init__(self, reason: str = EGRESS_BLOCKED) -> None:
        self.reason = reason
        super().__init__(reason)


def _patterns(raw: str) -> list[str]:
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def host_allowed(host: str, patterns: Sequence[str]) -> bool:
    h = host.lower().rstrip(".")
    return any(
        h == p or (p.startswith("*.") and fnmatch.fnmatchcase(h, p)) or p == "*" for p in patterns
    )


def _system_resolver(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]


def _blocked_ip(raw: str) -> bool:
    # Lazy import: url_capture pulls in the whole documents stack, which itself uses egress.
    from app.modules.connectors.url_capture import is_blocked_ip

    try:
        return is_blocked_ip(ipaddress.ip_address(raw.partition("%")[0]))
    except ValueError:
        return True


def _log(host: str, purpose: str, decision: str, *, level: int = logging.INFO) -> None:
    logger.log(level, "egress host=%s purpose=%s decision=%s", host, purpose, decision)


def check(
    host: str,
    purpose: str,
    *,
    addresses: Sequence[str] | None = None,
    resolver: Resolver | None = None,
    allowed_hosts: str | None = None,
) -> None:
    """Raise :class:`EgressDenied` unless an outbound call to ``host`` for ``purpose`` may go out.

    ``addresses`` are the already-resolved IPs (a pinning caller); otherwise user-URL purposes are
    resolved here. ``allowed_hosts`` overrides the setting (tests).
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        _log("-", purpose, "deny")
        raise EgressDenied()
    if purpose in USER_URL_PURPOSES:
        try:
            ips = (
                list(addresses)
                if addresses is not None
                else list((resolver or _system_resolver)(host))
            )
        except OSError:
            _log(host, purpose, "deny")
            raise EgressDenied() from None
        if not ips or any(_blocked_ip(ip) for ip in ips):
            _log(host, purpose, "deny")
            raise EgressDenied()
        _log(host, purpose, "allow")
        return
    raw = allowed_hosts if allowed_hosts is not None else get_settings().EGRESS_ALLOWED_HOSTS
    patterns = _patterns(raw)
    if not patterns:
        _log(host, purpose, "allow_unenforced", level=logging.WARNING)
        return
    if not host_allowed(host, patterns):
        _log(host, purpose, "deny")
        raise EgressDenied()
    _log(host, purpose, "allow")


def check_url(url: str, purpose: str, **kw: Any) -> None:
    check(urlsplit(url).hostname or "", purpose, **kw)


def request_hook(purpose: str) -> Callable[[Any], None]:
    """httpx ``request`` event hook for a client with a fixed purpose."""

    def hook(request: Any) -> None:
        check(request.url.host, purpose)

    return hook


# --- hosted providers: org opt-in and retention (M14.05) ----------------------------------------

HOSTED_NOT_ALLOWED: Final = "hosted_not_allowed"
HOSTED_PROVIDERS: Final = frozenset({"openai"})


def require_hosted_optin(provider: str, hosted_models_allowed: bool) -> None:
    """Refuse a hosted provider unless an owner opted the org in (``hosted_models_allowed``)."""
    if provider in HOSTED_PROVIDERS and not hosted_models_allowed:
        raise EgressDenied(HOSTED_NOT_ALLOWED)


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    """What we send to ask a provider not to retain, and what the provider is known to do."""

    body_params: dict[str, Any]
    headers: dict[str, str]
    note: str


# Notes marked 'verify' are unconfirmed against the current provider terms: confirm before relying.
PROVIDER_RETENTION: Final[dict[str, RetentionConfig]] = {
    "local": RetentionConfig({}, {}, "self-hosted endpoint: no third party; retention is ours."),
    "openai": RetentionConfig(
        {"store": False},
        {},
        "Chat Completions: store defaults to false; abuse-monitoring retention (up to ~30 days) "
        "applies unless the org has Zero Data Retention approved by OpenAI: verify per account.",
    ),
}


def zero_retention(provider: str) -> RetentionConfig | None:
    """Zero-retention parameters for a provider; ``None`` = unknown provider (treat as verify)."""
    return PROVIDER_RETENTION.get(provider)
