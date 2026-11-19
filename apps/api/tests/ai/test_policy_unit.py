"""Policy validation, shared M04 settings, routing of ``chat.run.queued`` (no database needed)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.models import ORG_SCOPED_TABLES, RLS_TABLES, Base
from app.modules.ai import models_db
from app.modules.ai.policy_store import (
    DEFAULT_POLICY,
    InvalidPolicy,
    ModelPolicy,
    validate_policy,
)
from app.modules.ingestion import models as ingestion_models
from app.workers.celery_app import QUEUE_CHAT, TASK_CHAT_RUN, make_celery
from app.workers.dispatcher import ROUTES, Delivery, OutboxRow, PoisonEvent
from app.workers.settings import WorkerSettings

# -- policy -----------------------------------------------------------------------------------


def test_default_policy_is_local_only() -> None:
    assert ModelPolicy(False, frozenset({"local"})) == DEFAULT_POLICY
    assert ModelPolicy() == DEFAULT_POLICY


@pytest.mark.parametrize(
    ("allow", "providers", "expected"),
    [
        (False, ["local"], ModelPolicy(False, frozenset({"local"}))),
        (True, ["local"], ModelPolicy(True, frozenset({"local"}))),
        (True, ["openai"], ModelPolicy(True, frozenset({"openai"}))),
        (True, ["local", "openai", "local"], ModelPolicy(True, frozenset({"local", "openai"}))),
    ],
)
def test_valid_policies(allow: bool, providers: list[str], expected: ModelPolicy) -> None:
    assert validate_policy(allow, providers) == expected


@pytest.mark.parametrize(
    ("allow", "providers"),
    [
        (True, []),
        (False, []),
        (False, ["openai"]),
        (False, ["local", "openai"]),
        (True, ["anthropic"]),
        (True, ["Local"]),
        (True, [""]),
    ],
)
def test_invalid_policies(allow: bool, providers: list[str]) -> None:
    with pytest.raises(InvalidPolicy):
        validate_policy(allow, providers)


def test_policy_is_immutable() -> None:
    with pytest.raises(AttributeError):
        DEFAULT_POLICY.allow_external = True  # type: ignore[misc]


# -- shared settings (contract §11) ----------------------------------------------------------


def test_m04_setting_defaults_follow_the_contract() -> None:
    s = Settings()
    assert (s.SEARCH_DEFAULT_LIMIT, s.SEARCH_CANDIDATES, s.SEARCH_MAX_PER_DOCUMENT) == (10, 50, 3)
    assert s.SEARCH_RATE_PER_MIN == 60
    assert s.CHAT_MODEL == "google/gemma-3-4b"
    assert (s.CHAT_CONTEXT_TOKENS, s.CHAT_MAX_OUTPUT_TOKENS, s.CHAT_TEMPERATURE) == (4096, 800, 0.0)
    assert (s.CHAT_HISTORY_TURNS, s.CHAT_RUN_TIMEOUT_SECONDS) == (6, 180)
    assert (s.CHAT_MAX_RUNS_PER_MIN, s.CHAT_MAX_CONCURRENT_RUNS) == (10, 2)
    assert (s.VERIFY_MODE, s.VERIFY_LEXICAL_MIN, s.VERIFY_EMBED_MIN) == ("strict", 0.35, 0.55)
    assert s.AI_EXTERNAL_PROVIDERS_ENABLED is False
    assert s.OPENAI_API_KEY is None
    assert s.OPENAI_CHAT_MODEL


@pytest.mark.parametrize(
    "bad",
    [
        {"SEARCH_DEFAULT_LIMIT": 0},
        {"SEARCH_DEFAULT_LIMIT": 51},
        {"SEARCH_CANDIDATES": 0},
        {"SEARCH_CANDIDATES": 5, "SEARCH_DEFAULT_LIMIT": 10},
        {"SEARCH_MAX_PER_DOCUMENT": 0},
        {"SEARCH_RATE_PER_MIN": 0},
        {"CHAT_MODEL": ""},
        {"CHAT_CONTEXT_TOKENS": 100},
        {"CHAT_MAX_OUTPUT_TOKENS": 0},
        {"CHAT_MAX_OUTPUT_TOKENS": 3000, "CHAT_CONTEXT_TOKENS": 4096},  # output > half the window
        {"CHAT_TEMPERATURE": -0.1},
        {"CHAT_TEMPERATURE": 2.5},
        {"CHAT_HISTORY_TURNS": -1},
        {"CHAT_RUN_TIMEOUT_SECONDS": 5},
        {"CHAT_MAX_RUNS_PER_MIN": 0},
        {"CHAT_MAX_CONCURRENT_RUNS": 0},
        {"VERIFY_MODE": "off"},
        {"VERIFY_LEXICAL_MIN": 1.5},
        {"VERIFY_LEXICAL_MIN": -0.1},
        {"VERIFY_EMBED_MIN": 2},
        {"AI_EXTERNAL_PROVIDERS_ENABLED": "sometimes"},
    ],
)
def test_invalid_m04_settings_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**bad)  # type: ignore[arg-type]


def test_valid_overrides_are_accepted() -> None:
    s = Settings(
        VERIFY_MODE="flag",
        CHAT_CONTEXT_TOKENS=8192,
        CHAT_MAX_OUTPUT_TOKENS=4096,  # exactly half is allowed
        CHAT_HISTORY_TURNS=0,
        AI_EXTERNAL_PROVIDERS_ENABLED=True,
    )
    assert (s.VERIFY_MODE, s.CHAT_HISTORY_TURNS, s.AI_EXTERNAL_PROVIDERS_ENABLED) == (
        "flag",
        0,
        True,
    )


def test_the_openai_key_is_secret_and_blank_means_unset() -> None:
    assert Settings(OPENAI_API_KEY="").OPENAI_API_KEY is None  # type: ignore[arg-type]
    assert Settings(OPENAI_API_KEY="   ").OPENAI_API_KEY is None  # type: ignore[arg-type]
    s = Settings(OPENAI_API_KEY="sk-test-not-real")  # type: ignore[arg-type]
    assert s.OPENAI_API_KEY is not None
    assert s.OPENAI_API_KEY.get_secret_value() == "sk-test-not-real"
    assert "sk-test-not-real" not in repr(s)
    assert "sk-test-not-real" not in str(s.model_dump())


def test_validation_errors_do_not_echo_the_key() -> None:
    with pytest.raises(ValidationError) as info:
        Settings(OPENAI_API_KEY="sk-leak-me", VERIFY_MODE="bogus")  # type: ignore[arg-type]
    assert "sk-leak-me" not in str(info.value)


# -- model registration ----------------------------------------------------------------------


def test_chat_tables_are_registered_org_scoped_rls_tables() -> None:
    assert set(models_db.CHAT_TABLES) <= set(ORG_SCOPED_TABLES)
    assert set(Base.metadata.tables) == set(RLS_TABLES)
    for name in models_db.CHAT_TABLES:
        assert name in Base.metadata.tables


def test_event_and_state_vocabulary_matches_the_contract() -> None:
    assert models_db.RUN_STATES == ("queued", "running", "completed", "failed", "cancelled")
    assert models_db.EVENT_TYPES == (
        "started",
        "retrieval",
        "delta",
        "citations",
        "verification",
        "completed",
        "failed",
        "cancelled",
    )
    assert models_db.MESSAGE_ROLES == ("user", "assistant")
    assert models_db.AI_PROVIDERS == ("local", "openai")


def test_outbox_event_type_is_in_both_places() -> None:
    assert models_db.EVENT_CHAT_RUN_QUEUED == ingestion_models.EVENT_CHAT_RUN_QUEUED
    assert "chat.run.queued" in ingestion_models.OUTBOX_EVENT_TYPES


# -- dispatcher routing and queues -----------------------------------------------------------


def _row(payload: dict[str, object]) -> OutboxRow:
    return OutboxRow(
        id=uuid.uuid4(),
        seq=1,
        org_id=uuid.uuid4(),
        aggregate_type="chat_run",
        aggregate_id=uuid.uuid4(),
        event_type="chat.run.queued",
        payload=payload,
        attempts=0,
    )


def test_chat_run_queued_routes_to_the_chat_queue() -> None:
    run = uuid.uuid4()
    assert ROUTES["chat.run.queued"](_row({"run_id": str(run)})) == Delivery(
        TASK_CHAT_RUN, QUEUE_CHAT, (str(run),)
    )
    assert (TASK_CHAT_RUN, QUEUE_CHAT) == ("chat.run", "chat")


@pytest.mark.parametrize("payload", [{}, {"run_id": "nope"}, {"job_id": str(uuid.uuid4())}])
def test_malformed_chat_payload_is_poison(payload: dict[str, object]) -> None:
    with pytest.raises(PoisonEvent):
        ROUTES["chat.run.queued"](_row(payload))


def test_celery_declares_and_routes_the_chat_queue() -> None:
    conf = make_celery(WorkerSettings()).conf
    assert QUEUE_CHAT in {q.name for q in conf.task_queues}
    assert conf.task_routes[TASK_CHAT_RUN]["queue"] == QUEUE_CHAT
