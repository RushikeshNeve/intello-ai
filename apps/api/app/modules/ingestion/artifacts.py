"""Stage artifacts in the artifacts bucket (M03 contract §6, M03.02.04).

Layout: ``org/<org_id>/rev/<revision_id>/<file>`` where ``<file>`` is ``extracted.json``,
``chunks.json`` or ``embeddings.f32``. The key is derived only from two canonical UUIDs, so the
revision (which already encodes the version, pipeline version and embedding space) fully
identifies the objects and two spaces can never collide.

Interpretation of the contract (see the handoff): the contract text spells the key
``org/<org>/doc/<doc>/<version>/<pipeline_version>/<stage>.<ext>`` and says "written with
``If-None-Match``". A key without the embedding space would let two spaces overwrite each other,
and a corrupted artifact must be *replaceable* on resume, so objects are keyed by revision and
written unconditionally (idempotent: identical inputs give identical bytes and an identical key).

Every read is verified against the sha256 recorded in the checkpoint: a missing object, a size cap
violation or a digest mismatch is reported as ``None`` ("recompute"), never trusted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.modules.ingestion.contracts import Chunk, ExtractedDocument, canonical_json_bytes
from app.modules.storage.ports import ObjectNotFound, ObjectStore

STAGE_FILES: Final = {
    "extract": ("extracted.json", "application/json"),
    "chunk": ("chunks.json", "application/json"),
    "embed": ("embeddings.f32", "application/octet-stream"),
}
ARTIFACT_STAGES: Final = tuple(STAGE_FILES)
MAX_ARTIFACT_BYTES: Final = 512 * 1024 * 1024
CHUNKS_SCHEMA_VERSION: Final = 1


def artifact_key(org_id: uuid.UUID, revision_id: uuid.UUID, stage: str) -> str:
    """Key of a stage artifact. Only artifact stages (extract, chunk, embed) have one."""
    try:
        filename, _ = STAGE_FILES[stage]
    except KeyError:
        raise ValueError(f"stage {stage!r} has no artifact") from None
    return f"org/{org_id}/rev/{revision_id}/{filename}"


def artifact_content_type(stage: str) -> str:
    return STAGE_FILES[stage][1]


def revision_artifact_keys(org_id: uuid.UUID, revision_id: uuid.UUID) -> list[str]:
    return [artifact_key(org_id, revision_id, stage) for stage in ARTIFACT_STAGES]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


# ------------------------------------------------------------------------------------- codecs


def encode_extracted(doc: ExtractedDocument) -> bytes:
    return doc.to_canonical_json()


def decode_extracted(data: bytes) -> ExtractedDocument:
    return ExtractedDocument.from_json(data)


def encode_chunks(chunks: Sequence[Chunk]) -> bytes:
    payload: dict[str, Any] = {
        "schema_version": CHUNKS_SCHEMA_VERSION,
        "chunks": [c.model_dump(mode="json", exclude_none=True) for c in chunks],
    }
    return canonical_json_bytes(payload)


def decode_chunks(data: bytes) -> list[Chunk]:
    payload = json.loads(data)
    if not isinstance(payload, dict) or payload.get("schema_version") != CHUNKS_SCHEMA_VERSION:
        raise ValueError("unsupported chunks artifact")
    items = payload.get("chunks")
    if not isinstance(items, list):
        raise ValueError("malformed chunks artifact")
    return [Chunk.from_json(canonical_json_bytes(item)) for item in items]


def encode_embeddings(vectors: Sequence[Sequence[float]], dimensions: int) -> bytes:
    """``count x dimensions`` little-endian float32, row ``i`` = chunk ``i`` (contract §7)."""
    out = bytearray()
    for vector in vectors:
        if len(vector) != dimensions:
            raise ValueError("vector has the wrong number of dimensions")
        out += struct.pack(f"<{dimensions}f", *vector)
    return bytes(out)


def decode_embeddings(data: bytes, count: int, dimensions: int) -> list[list[float]]:
    row = 4 * dimensions
    if dimensions < 1 or len(data) != count * row:
        raise ValueError("embeddings artifact has the wrong size")
    return [list(struct.unpack_from(f"<{dimensions}f", data, i * row)) for i in range(count)]


# ----------------------------------------------------------------------------------- the store


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    key: str
    sha256: str
    size: int


class ArtifactStore:
    """Thin wrapper over an :class:`ObjectStore` bound to the artifacts bucket."""

    def __init__(self, store: ObjectStore, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> None:
        self._store = store
        self._max_bytes = max_bytes

    def put(self, key: str, data: bytes, *, content_type: str) -> StoredArtifact:
        """Write ``data`` (overwriting; identical bytes make this idempotent)."""
        self._store.put_object(
            key, data, content_type=content_type, sha256_b64=_b64(data), if_none_match=False
        )
        return StoredArtifact(key, sha256_hex(data), len(data))

    def get_verified(self, key: str, *, sha256: str | None, size: int | None) -> bytes | None:
        """The object's bytes if it exists and matches ``sha256``/``size``; otherwise ``None``.

        ``StorageUnavailable`` propagates (a store outage is not a reason to recompute).
        """
        if sha256 is None:
            return None
        try:
            chunks = self._store.get_object(key)
        except ObjectNotFound:
            return None
        buf = bytearray()
        try:
            for part in chunks:
                buf += part
                if len(buf) > self._max_bytes or (size is not None and len(buf) > size):
                    return None
        except ObjectNotFound:
            return None
        finally:
            close = getattr(chunks, "close", None)
            if callable(close):
                close()
        data = bytes(buf)
        if (size is not None and len(data) != size) or sha256_hex(data) != sha256:
            return None
        return data

    def delete_revision(self, org_id: uuid.UUID, revision_id: uuid.UUID) -> None:
        """Remove every artifact of a revision (idempotent)."""
        for key in revision_artifact_keys(org_id, revision_id):
            self._store.delete_object(key)
