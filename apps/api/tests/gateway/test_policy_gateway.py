"""M04.02.01 acceptance: model policy denies forbidden providers (single choke point)."""

from __future__ import annotations

import itertools
import logging
from uuid import uuid4

import pytest

from app.modules.ai.gateway import (
    ChatModel,
    FakeChatModel,
    InMemoryPolicySource,
    LocalChatModel,
    ModelPolicy,
    OpenAIChatModel,
    PolicyDenied,
    get_chat_model,
)
from app.modules.ai.models.policy import DENIAL_MESSAGES
from tests.gateway.support import settings

SECRET = "sk-test-SECRET-1234567890"
LOCAL_URL = "http://127.0.0.1:1234/v1"
REMOTE_URL = "https://models.example.com/v1"


def _model(
    env_enabled: bool,
    allow_external: bool,
    providers: frozenset[str],
    key: bool,
    provider: str | None,
    base_url: str,
) -> ChatModel:
    org = uuid4()
    src = InMemoryPolicySource({org: ModelPolicy(allow_external, providers, True)})
    s = settings(
        AI_EXTERNAL_PROVIDERS_ENABLED=env_enabled,
        OPENAI_API_KEY=SECRET if key else "",
        AI_BASE_URL=base_url,
    )
    return get_chat_model(src, org, s, provider=provider)  # type: ignore[arg-type]


def _expected(
    env_enabled: bool,
    allow_external: bool,
    providers: frozenset[str],
    key: bool,
    provider: str | None,
    base_url: str,
) -> str:
    """Independent statement of the matrix: a class name, or a denial reason."""
    external = env_enabled and allow_external
    local_host = base_url == LOCAL_URL
    chosen = provider
    if chosen is None:
        chosen = "local" if "local" in providers else ("openai" if "openai" in providers else None)
        if chosen is None:
            return "no_allowed_provider"
    if chosen == "openai":
        if not env_enabled:
            return "external_disabled"
        if not allow_external:
            return "org_policy_denies_external"
        if "openai" not in providers:
            return "provider_not_allowed"
        if not key:
            return "missing_credentials"
        return "OpenAIChatModel"
    if "local" not in providers:
        return "provider_not_allowed"
    if not local_host and not external:
        return "endpoint_not_local"
    return "LocalChatModel"


PROVIDER_SETS = [
    frozenset(),
    frozenset({"local"}),
    frozenset({"openai"}),
    frozenset({"local", "openai"}),
]


@pytest.mark.parametrize(
    ("env_enabled", "allow_external", "providers", "key", "provider", "base_url"),
    list(
        itertools.product(
            [False, True],
            [False, True],
            PROVIDER_SETS,
            [False, True],
            [None, "local", "openai"],
            [LOCAL_URL, REMOTE_URL],
        )
    ),
)
def test_policy_matrix(env_enabled, allow_external, providers, key, provider, base_url):  # type: ignore[no-untyped-def]
    expected = _expected(env_enabled, allow_external, providers, key, provider, base_url)
    if expected in DENIAL_MESSAGES:
        with pytest.raises(PolicyDenied) as err:
            _model(env_enabled, allow_external, providers, key, provider, base_url)
        assert err.value.reason == expected
        assert SECRET not in str(err.value)
        assert SECRET not in repr(err.value)
        assert "http" not in str(err.value)
    else:
        model = _model(env_enabled, allow_external, providers, key, provider, base_url)
        assert type(model).__name__ == expected
        assert SECRET not in repr(model)


def test_default_is_local_only_without_any_configuration() -> None:
    org = uuid4()
    model = get_chat_model(InMemoryPolicySource(), org, settings(AI_BASE_URL=LOCAL_URL))
    assert isinstance(model, LocalChatModel)
    assert model.spec.provider == "local"
    assert model.spec.model_id == "google/gemma-3-4b"


def test_openai_constructed_only_when_all_gates_open() -> None:
    org = uuid4()
    src = InMemoryPolicySource({org: ModelPolicy(True, frozenset({"openai"}), True)})
    s = settings(AI_EXTERNAL_PROVIDERS_ENABLED=True, OPENAI_API_KEY=SECRET)
    model = get_chat_model(src, org, s)
    assert isinstance(model, OpenAIChatModel)
    assert model.spec.provider == "openai"


def test_policy_change_applies_to_next_call() -> None:
    org = uuid4()
    src = InMemoryPolicySource()
    s = settings(AI_EXTERNAL_PROVIDERS_ENABLED=True, OPENAI_API_KEY=SECRET, AI_BASE_URL=LOCAL_URL)
    with pytest.raises(PolicyDenied):
        get_chat_model(src, org, s, provider="openai")
    src.set_policy(org, ModelPolicy(True, frozenset({"local", "openai"}), True))
    assert isinstance(get_chat_model(src, org, s, provider="openai"), OpenAIChatModel)
    src.set_policy(org, ModelPolicy.local_only())
    with pytest.raises(PolicyDenied):
        get_chat_model(src, org, s, provider="openai")


def test_policy_source_failure_fails_closed() -> None:
    class Broken:
        def get_policy(self, org_id: object) -> ModelPolicy:
            raise RuntimeError(f"db down {SECRET}")

    with pytest.raises(PolicyDenied) as err:
        get_chat_model(Broken(), uuid4(), settings())
    assert err.value.reason == "policy_unavailable"
    assert SECRET not in str(err.value)
    assert err.value.__cause__ is None


def test_unknown_provider_denied() -> None:
    with pytest.raises(PolicyDenied) as err:
        get_chat_model(InMemoryPolicySource(), uuid4(), settings(), provider="anthropic")  # type: ignore[arg-type]
    assert err.value.reason == "unknown_provider"


def test_placeholder_and_blank_keys_count_as_missing() -> None:
    org = uuid4()
    src = InMemoryPolicySource({org: ModelPolicy(True, frozenset({"openai"}), True)})
    for key in ("", "   ", "change-me-openai"):
        s = settings(AI_EXTERNAL_PROVIDERS_ENABLED=True, OPENAI_API_KEY=key)
        with pytest.raises(PolicyDenied) as err:
            get_chat_model(src, org, s)
        assert err.value.reason == "missing_credentials"


def test_settings_repr_and_logs_hide_keys(caplog: pytest.LogCaptureFixture) -> None:
    s = settings(OPENAI_API_KEY=SECRET, AI_API_KEY=SECRET)
    assert SECRET not in repr(s)
    assert SECRET not in str(s)
    with caplog.at_level(logging.DEBUG):
        org = uuid4()
        src = InMemoryPolicySource({org: ModelPolicy(True, frozenset({"openai"}), True)})
        s2 = settings(AI_EXTERNAL_PROVIDERS_ENABLED=True, OPENAI_API_KEY=SECRET)
        model = get_chat_model(src, org, s2)
        assert SECRET not in repr(model)
    assert SECRET not in caplog.text


def test_fake_model_records_calls_and_scripts_replies() -> None:
    fake = FakeChatModel(["hello world", "second"])
    msgs = [{"role": "user", "content": "q"}]
    assert list(fake.stream(msgs, stop=["x"])) == ["hello w", "orld"]  # type: ignore[arg-type]
    assert fake.complete(msgs) == "second"  # type: ignore[arg-type]
    assert fake.complete(msgs) == "second"  # type: ignore[arg-type]
    assert len(fake.calls) == 3
    assert fake.calls[0].stop == ("x",)
