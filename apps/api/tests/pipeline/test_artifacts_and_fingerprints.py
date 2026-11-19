"""Unit tests of artifact keys/codecs/verification and stage fingerprints (no database)."""

from __future__ import annotations

import base64
import hashlib
import struct
import uuid

import pytest

from app.infrastructure.object_storage.memory import InMemoryObjectStore
from app.modules.ingestion import artifacts as art
from app.modules.ingestion.checkpoints import Checkpoint, reusable, stage_fingerprint
from app.modules.ingestion.ids import chunk_id_for

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
REV = uuid.UUID("22222222-2222-2222-2222-222222222222")


def test_artifact_keys_follow_the_layout() -> None:
    assert art.artifact_key(ORG, REV, "extract") == f"org/{ORG}/rev/{REV}/extracted.json"
    assert art.artifact_key(ORG, REV, "chunk") == f"org/{ORG}/rev/{REV}/chunks.json"
    assert art.artifact_key(ORG, REV, "embed") == f"org/{ORG}/rev/{REV}/embeddings.f32"
    with pytest.raises(ValueError, match="no artifact"):
        art.artifact_key(ORG, REV, "index")


def test_embeddings_are_little_endian_float32_row_major() -> None:
    data = art.encode_embeddings([[1.0, -2.5], [0.25, 3.0]], 2)
    assert data == struct.pack("<4f", 1.0, -2.5, 0.25, 3.0)
    assert art.decode_embeddings(data, 2, 2) == [[1.0, -2.5], [0.25, 3.0]]
    with pytest.raises(ValueError, match="wrong size"):
        art.decode_embeddings(data, 3, 2)
    with pytest.raises(ValueError, match="wrong number"):
        art.encode_embeddings([[1.0]], 2)


def test_chunks_round_trip_canonically() -> None:
    from app.modules.ingestion.contracts import Chunk, Locator

    text = "hello world"
    chunk = Chunk(
        chunk_index=0,
        chunk_id=chunk_id_for(REV, 0),
        text=text,
        token_estimate=3,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        locators=[Locator(kind="offset", char_start=0, char_end=11)],
        heading_path=[],
    )
    data = art.encode_chunks([chunk])
    assert art.decode_chunks(data) == [chunk]
    assert art.encode_chunks(art.decode_chunks(data)) == data  # byte-identical re-run


def test_verified_read_rejects_missing_flipped_and_wrong_size() -> None:
    store = InMemoryObjectStore()
    arts = art.ArtifactStore(store)
    key = art.artifact_key(ORG, REV, "chunk")
    assert arts.get_verified(key, sha256="0" * 64, size=1) is None  # missing
    data = b'{"a":1}'
    stored = arts.put(key, data, content_type="application/json")
    assert arts.get_verified(key, sha256=stored.sha256, size=stored.size) == data
    bad = b'{"a":2}'
    store.put_object(
        key,
        bad,
        content_type="application/json",
        sha256_b64=base64.b64encode(hashlib.sha256(bad).digest()).decode(),
        if_none_match=False,
    )
    assert arts.get_verified(key, sha256=stored.sha256, size=stored.size) is None  # flipped
    assert arts.get_verified(key, sha256=hashlib.sha256(bad).hexdigest(), size=99) is None
    assert arts.get_verified(key, sha256=None, size=None) is None


def test_put_is_idempotent_and_delete_revision_removes_all() -> None:
    store = InMemoryObjectStore()
    arts = art.ArtifactStore(store)
    for stage in art.ARTIFACT_STAGES:
        arts.put(art.artifact_key(ORG, REV, stage), b"x", content_type="text/plain")
        arts.put(art.artifact_key(ORG, REV, stage), b"x", content_type="text/plain")
    arts.delete_revision(ORG, REV)
    arts.delete_revision(ORG, REV)
    assert all(store.head_object(k) is None for k in art.revision_artifact_keys(ORG, REV))


def test_fingerprint_depends_on_stage_input_and_every_parameter() -> None:
    base = stage_fingerprint("chunk", "a" * 64, {"target": 1800, "pv": "1"})
    assert base == stage_fingerprint("chunk", "a" * 64, {"pv": "1", "target": 1800})
    assert base != stage_fingerprint("embed", "a" * 64, {"target": 1800, "pv": "1"})
    assert base != stage_fingerprint("chunk", "b" * 64, {"target": 1800, "pv": "1"})
    assert base != stage_fingerprint("chunk", "a" * 64, {"target": 1801, "pv": "1"})
    assert base != stage_fingerprint("chunk", "a" * 64, {"target": 1800, "pv": "2"})


def test_reusable_needs_completed_and_equal_fingerprint() -> None:
    def cp(state: str, fp: str) -> Checkpoint:
        return Checkpoint("embed", state, fp, "k", "s", 1, 1)

    assert reusable(cp("completed", "f"), "f")
    assert not reusable(cp("completed", "g"), "f")
    assert not reusable(cp("running", "f"), "f")
    assert not reusable(cp("failed", "f"), "f")
    assert not reusable(None, "f")
