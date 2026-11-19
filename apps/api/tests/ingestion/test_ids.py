"""The ingestion id formulas are a persistent contract: a change silently re-keys everything.

The pinned values below were produced by the functions themselves on 2026-11-19. If a test here
fails, do NOT update the expected value to make it pass: bump ``PIPELINE_VERSION`` (or the
embedding ``spec_version``) instead.
"""

from __future__ import annotations

import uuid

import pytest

from app.modules.ingestion.ids import (
    INGEST_NAMESPACE,
    PIPELINE_VERSION,
    chunk_id_for,
    compute_embedding_space_id,
    revision_id_for,
)

VERSION_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
QUERY_PREFIX = "task: search result | query: "
DOCUMENT_PREFIX = "title: none | text: "
GEMMA_SPACE = compute_embedding_space_id(
    "google/embedding-gemma-300m", 768, QUERY_PREFIX, DOCUMENT_PREFIX, 1
)


def test_pinned_reference_values() -> None:
    assert str(INGEST_NAMESPACE) == "8e4127cb-cae1-57ea-b6b7-5bf64a0929bc"
    assert PIPELINE_VERSION == "1"
    assert GEMMA_SPACE == "eb3ad37f8d916"
    revision = revision_id_for(VERSION_ID, PIPELINE_VERSION, GEMMA_SPACE)
    assert str(revision) == "3f99ea5c-ac55-513c-a62a-4d5f69d7ba5d"
    assert str(chunk_id_for(revision, 0)) == "8e0f74f5-9484-5f84-ab3a-d244d6283c77"
    assert str(chunk_id_for(revision, 7)) == "02c24ab7-b22c-5002-afd2-f64a570d4790"


def test_revision_id_is_deterministic_and_input_sensitive() -> None:
    a = revision_id_for(VERSION_ID, "1", GEMMA_SPACE)
    assert a == revision_id_for(VERSION_ID, "1", GEMMA_SPACE)
    assert a != revision_id_for(uuid.uuid4(), "1", GEMMA_SPACE)  # another version
    assert a != revision_id_for(VERSION_ID, "2", GEMMA_SPACE)  # another pipeline
    assert a != revision_id_for(VERSION_ID, "1", "e000000000000")  # another embedding space


def test_chunk_ids_are_unique_stable_and_scoped_to_the_revision() -> None:
    revision = revision_id_for(VERSION_ID, "1", GEMMA_SPACE)
    ids = [chunk_id_for(revision, i) for i in range(2000)]
    assert len(set(ids)) == 2000
    assert ids == [chunk_id_for(revision, i) for i in range(2000)]
    other = revision_id_for(VERSION_ID, "2", GEMMA_SPACE)
    assert chunk_id_for(other, 0) != ids[0]


def test_negative_chunk_index_is_rejected() -> None:
    with pytest.raises(ValueError, match="chunk_index"):
        chunk_id_for(uuid.uuid4(), -1)


@pytest.mark.parametrize(
    "changed",
    [
        ("other/model", 768, QUERY_PREFIX, DOCUMENT_PREFIX, 1),
        ("google/embedding-gemma-300m", 1024, QUERY_PREFIX, DOCUMENT_PREFIX, 1),
        ("google/embedding-gemma-300m", 768, "query: ", DOCUMENT_PREFIX, 1),
        ("google/embedding-gemma-300m", 768, QUERY_PREFIX, "doc: ", 1),
        ("google/embedding-gemma-300m", 768, QUERY_PREFIX, DOCUMENT_PREFIX, 2),
    ],
    ids=["model", "dimensions", "query_prefix", "document_prefix", "spec_version"],
)
def test_every_part_of_an_embedding_space_matters(changed: tuple[str, int, str, str, int]) -> None:
    assert compute_embedding_space_id(*changed) != GEMMA_SPACE


def test_space_id_shape_and_validation() -> None:
    assert GEMMA_SPACE.startswith("e")
    assert len(GEMMA_SPACE) == 13
    with pytest.raises(ValueError, match="dimensions"):
        compute_embedding_space_id("m", 0, "", "", 1)
