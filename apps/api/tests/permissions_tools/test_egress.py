"""M14.05: egress allowlist, private-range blocking, hosted opt-in, every client goes through it."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.modules.ai import egress
from app.modules.ai.gateway import ChatSettings, InMemoryPolicySource, ModelPolicy, get_chat_model
from app.modules.ai.models.policy import PolicyDenied

APP_DIR = Path(__file__).resolve().parents[2] / "app"
ALLOW = "api.openai.com,*.github.com,rushdb"


def test_allowlist_exact_wildcard_and_block() -> None:
    egress.check("api.openai.com", "model:openai", allowed_hosts=ALLOW)
    egress.check("API.github.com.", "connector:github", allowed_hosts=ALLOW)
    for bad in ("evil.example", "github.com.evil.example", "openai.com"):
        with pytest.raises(egress.EgressDenied):
            egress.check(bad, "model:openai", allowed_hosts=ALLOW)


def test_empty_allowlist_allows_all_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="ukos.egress"):
        egress.check("anything.example", "warmup", allowed_hosts="")
    assert "allow_unenforced" in caplog.text


def test_user_url_dns_rebinding_to_private_ip_is_blocked() -> None:
    def rebinding(_host: str) -> list[str]:
        return ["93.184.216.34", "169.254.169.254"]  # one public answer, one metadata answer

    with pytest.raises(egress.EgressDenied):
        egress.check("rebind.example", "url_capture", resolver=rebinding)
    egress.check("ok.example", "url_capture", resolver=lambda _h: ["93.184.216.34"])
    for ip in ("127.0.0.1", "10.0.0.5", "fd00::1", "::ffff:169.254.169.254"):
        with pytest.raises(egress.EgressDenied):
            egress.check("x.example", "url_capture", addresses=[ip])


def test_logs_only_host_purpose_decision(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="ukos.egress"):
        egress.check("api.openai.com", "model:openai", allowed_hosts=ALLOW)
    assert "host=api.openai.com purpose=model:openai decision=allow" in caplog.text


def test_request_hook_blocks_unlisted_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EGRESS_ALLOWED_HOSTS", "good.example")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200)),
            event_hooks={"request": [egress.request_hook("test")]},
        )
        assert client.get("https://good.example/x").status_code == 200
        with pytest.raises(egress.EgressDenied):
            client.get("https://bad.example/x")
    finally:
        get_settings.cache_clear()


def _settings() -> ChatSettings:
    return ChatSettings(AI_EXTERNAL_PROVIDERS_ENABLED=True, OPENAI_API_KEY="sk-test")  # type: ignore[arg-type]


def test_org_without_optin_cannot_use_hosted_provider() -> None:
    org = uuid4()
    no_optin = InMemoryPolicySource({org: ModelPolicy(True, frozenset({"openai"}))})
    with pytest.raises(PolicyDenied) as exc:
        get_chat_model(no_optin, org, _settings(), provider="openai")
    assert exc.value.reason == "hosted_not_allowed"
    opted = InMemoryPolicySource({org: ModelPolicy(True, frozenset({"openai"}), True)})
    assert get_chat_model(opted, org, _settings(), provider="openai") is not None
    with pytest.raises(egress.EgressDenied):
        egress.require_hosted_optin("openai", False)
    egress.require_hosted_optin("local", False)


def test_retention_config_known_and_unknown() -> None:
    cfg = egress.zero_retention("openai")
    assert cfg is not None
    assert cfg.body_params == {"store": False}
    assert "verify" in cfg.note
    assert egress.zero_retention("mystery") is None


# modules that create an outbound HTTP client; each MUST reference the egress module.
CLIENT_MODULES = {
    "infrastructure/rushdb/repository.py",
    "modules/ai/embeddings/client.py",
    "modules/ai/models/wire.py",
    "modules/ai/warmup.py",
    "modules/connectors/url_capture.py",
    "modules/connectors/wiring.py",
    "modules/identity/oidc.py",
}
CLIENT_PATTERN = re.compile(
    r"httpx\.(Async)?Client\(|httpx\.(get|post|put|delete|request|stream)\(|"
    r"^\s*import requests|urllib\.request|aiohttp",
    re.MULTILINE,
)


def test_every_outbound_client_goes_through_egress() -> None:
    found = {
        p.relative_to(APP_DIR).as_posix()
        for p in APP_DIR.rglob("*.py")
        if CLIENT_PATTERN.search(p.read_text(encoding="utf-8"))
    }
    assert found == CLIENT_MODULES, f"new/removed outbound client module: {found ^ CLIENT_MODULES}"
    for rel in CLIENT_MODULES:
        assert "egress." in (APP_DIR / rel).read_text(encoding="utf-8"), rel
    assert "egress.check" in (APP_DIR / "core/tracing.py").read_text(encoding="utf-8")
