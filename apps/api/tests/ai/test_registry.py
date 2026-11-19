"""Registry and settings: the default space id is pinned; every identity field changes the id."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.modules.ai.embeddings.registry import (
    DEFAULT_SPEC,
    EmbeddingModelSpec,
    spec_from_settings,
)
from app.modules.ai.embeddings.settings import EmbeddingSettings
from app.modules.ingestion.ids import compute_embedding_space_id

PINNED_DEFAULT_SPACE = "eb3ad37f8d916"


def test_default_spec_is_contract_section_7() -> None:
    assert DEFAULT_SPEC.provider == "openai_compatible"
    assert DEFAULT_SPEC.model_id == "google/embedding-gemma-300m"
    assert DEFAULT_SPEC.dimensions == 768
    assert DEFAULT_SPEC.query_prefix == "task: search result | query: "
    assert DEFAULT_SPEC.document_prefix == "title: none | text: "
    assert DEFAULT_SPEC.spec_version == 1
    assert DEFAULT_SPEC.normalize is True
    assert DEFAULT_SPEC.max_input_chars > 0


def test_default_space_id_is_pinned_and_uses_the_shared_formula() -> None:
    assert DEFAULT_SPEC.space_id == PINNED_DEFAULT_SPACE
    assert DEFAULT_SPEC.space_id == compute_embedding_space_id(
        "google/embedding-gemma-300m",
        768,
        "task: search result | query: ",
        "title: none | text: ",
        1,
    )


def test_spec_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_SPEC.dimensions = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "change",
    [
        {"model_id": "other/model"},
        {"dimensions": 767},
        {"query_prefix": "q: "},
        {"document_prefix": "d: "},
        {"spec_version": 2},
    ],
)
def test_every_identity_field_changes_the_space_id(change: dict[str, Any]) -> None:
    changed = dataclasses.replace(DEFAULT_SPEC, **change)
    assert changed.space_id != DEFAULT_SPEC.space_id


@pytest.mark.parametrize("change", [{"max_input_chars": 100}, {"normalize": False}])
def test_non_identity_fields_do_not_change_the_space_id(change: dict[str, Any]) -> None:
    assert dataclasses.replace(DEFAULT_SPEC, **change).space_id == DEFAULT_SPEC.space_id


@pytest.mark.parametrize(
    "change",
    [
        {"dimensions": 0},
        {"dimensions": -5},
        {"dimensions": 8193},
        {"model_id": "  "},
        {"max_input_chars": 0},
        {"spec_version": 0},
        {"provider": "carrier-pigeon"},
    ],
)
def test_invalid_specs_are_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"."):
        dataclasses.replace(DEFAULT_SPEC, **change)


def test_settings_defaults_map_to_the_default_spec() -> None:
    settings = EmbeddingSettings()
    assert spec_from_settings(settings) == DEFAULT_SPEC
    assert settings.INGEST_EMBED_BATCH_SIZE == 16
    assert settings.AI_EXTERNAL_PROVIDERS_ENABLED is False
    assert settings.AI_API_KEY is None


def test_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "AI_BASE_URL": "http://localhost:9999/v1",
        "AI_API_KEY": "sk-test-1",
        "AI_EMBEDDING_MODEL": "acme/embed-x",
        "AI_EMBEDDING_DIMENSIONS": "1024",
        "AI_EMBEDDING_QUERY_PREFIX": "Q: ",
        "AI_EMBEDDING_DOCUMENT_PREFIX": "",
        "AI_EMBEDDING_SPEC_VERSION": "3",
        "AI_EMBEDDING_MAX_INPUT_CHARS": "500",
        "AI_EMBEDDING_NORMALIZE": "false",
        "INGEST_EMBED_BATCH_SIZE": "4",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = EmbeddingSettings()
    assert isinstance(settings.AI_API_KEY, SecretStr)
    assert "sk-test-1" not in repr(settings)
    assert settings.INGEST_EMBED_BATCH_SIZE == 4
    spec = spec_from_settings(settings)
    assert spec == EmbeddingModelSpec(
        model_id="acme/embed-x",
        dimensions=1024,
        query_prefix="Q: ",
        document_prefix="",
        max_input_chars=500,
        spec_version=3,
        normalize=False,
    )
    assert spec.space_id == compute_embedding_space_id("acme/embed-x", 1024, "Q: ", "", 3)


def test_blank_api_key_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_API_KEY", "  ")
    assert EmbeddingSettings().AI_API_KEY is None


@pytest.mark.parametrize("value", ["0", "-1", "99999", "abc"])
def test_bad_dimensions_in_settings_are_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("AI_EMBEDDING_DIMENSIONS", value)
    with pytest.raises(ValidationError):
        EmbeddingSettings()


def test_spec_from_a_foreign_settings_object_uses_defaults_for_missing_fields() -> None:
    class Shared:
        AI_EMBEDDING_MODEL = "google/embedding-gemma-300m"
        AI_EMBEDDING_DIMENSIONS = 768

    assert spec_from_settings(Shared()) == DEFAULT_SPEC
