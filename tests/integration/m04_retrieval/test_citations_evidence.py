"""M04.01.04: citations resolve to the cited, authorized version; the evidence endpoint reads the
artifact store (sha256-verified), never the index."""

from __future__ import annotations

import uuid

import pytest
from catalog_support import World
from ret_support import Published, Rig, chunk_path
from sqlalchemy import text

from app.modules.retrieval import (
    CitationRef,
    EmptyScope,
    ResolvedCitation,
    resolve_citation,
    resolve_scope,
)
from app.modules.storage.ports import StorageUnavailable

TEXTS = [
    "First chunk of the quarterly report about herons.",
    "Second chunk continues the story of the herons.",
    "Third chunk closes the report.",
]


def evidence_url(world: World, pub: Published, chunk_id: uuid.UUID | None = None) -> str:
    return chunk_path(
        world.org_id, world.ws, pub.document_id, pub.version_id, chunk_id or pub.chunk_ids[1]
    )


def resolve(
    rig: Rig, world: World, ref: CitationRef, who: str = "owner"
) -> ResolvedCitation | None:
    user = world.t.owner if who == "owner" else world.viewer
    with rig.tenant(user, world.org_id) as session:
        scope = resolve_scope(session, org_id=world.org_id, user_id=user.id)
        return resolve_citation(session, scope, ref)


def ref_of(pub: Published, index: int = 0) -> CitationRef:
    return CitationRef(
        chunk_id=pub.chunk_ids[index], document_id=pub.document_id, version_id=pub.version_id
    )


# --------------------------------------------------------------------------- the endpoint


def test_evidence_reads_the_artifact_not_the_index(rig: Rig, world: World) -> None:
    doc = rig.seed_world(world, title="Report")
    pub = rig.publish(
        world.org_id,
        world.ws,
        doc,
        TEXTS,
        index_texts=["STALE INDEX TEXT one", "STALE INDEX TEXT two", "STALE INDEX TEXT three"],
    )
    res = world.api.get(evidence_url(world, pub), world.viewer)
    assert res.status == 200, res.body
    assert res.body["text"] == TEXTS[1]  # artifact text, not the index's stale copy
    assert res.body["chunk_id"] == str(pub.chunk_ids[1])
    assert res.body["title"] == "Report"
    assert res.body["version_number"] == 1
    assert res.body["heading_path"] == ["Heading"]
    assert res.body["locators"][0]["kind"] == "line"
    assert res.body["neighbors"] == {
        "prev_chunk_id": str(pub.chunk_ids[0]),
        "next_chunk_id": str(pub.chunk_ids[2]),
    }
    first = world.api.get(evidence_url(world, pub, pub.chunk_ids[0]), world.viewer)
    assert first.body["neighbors"] == {
        "prev_chunk_id": None,
        "next_chunk_id": str(pub.chunk_ids[1]),
    }


def test_evidence_is_authorized_as_document_read(rig: Rig, world: World) -> None:
    doc = rig.seed_world(world, title="Report")
    pub = rig.publish(world.org_id, world.ws, doc, TEXTS)
    url = evidence_url(world, pub)
    for user in (world.t.owner, world.t.admin, world.viewer, world.editor, world.ws_admin):
        assert world.api.get(url, user).status == 200
    assert world.api.get(url, world.plain).status == 404  # no role, no grant: concealed
    assert world.api.get(url, world.t.outsider).status == 404
    assert world.api.get(url, None).status == 401
    world.grant(doc.id, world.plain, "read")  # grants open a single document (and its evidence)
    assert world.api.get(url, world.plain).status == 200


def test_evidence_never_returns_another_versions_or_documents_chunk(rig: Rig, world: World) -> None:
    doc = rig.seed_world(world, title="Two versions", versions=2)
    v1 = rig.publish(world.org_id, world.ws, doc, TEXTS)
    v2 = rig.publish(world.org_id, world.ws, doc, ["Version two text only."], version=2)
    other = rig.seed_world(world, title="Other")
    other_pub = rig.publish(world.org_id, world.ws, other, ["Other document text."])
    # v1 chunk asked through v2's version id, and the reverse: 404
    assert (
        world.api.get(
            chunk_path(world.org_id, world.ws, doc.id, v2.version_id, v1.chunk_ids[0]),
            world.viewer,
        ).status
        == 404
    )
    assert (
        world.api.get(
            chunk_path(world.org_id, world.ws, doc.id, v1.version_id, v2.chunk_ids[0]),
            world.viewer,
        ).status
        == 404
    )
    # another document's chunk through this document: 404; random chunk id: 404
    for chunk in (other_pub.chunk_ids[0], uuid.uuid4()):
        res = world.api.get(
            chunk_path(world.org_id, world.ws, doc.id, v1.version_id, chunk), world.viewer
        )
        assert res.status == 404
    # the superseded version's chunk still opens through its own version
    assert world.api.get(evidence_url(world, v1), world.viewer).body["text"] == TEXTS[1]
    # a deleted document is 404 for everyone
    with rig.admin.begin() as conn:
        conn.execute(
            text("UPDATE documents SET deleted_at = now(), deleted_by = created_by WHERE id = :d"),
            {"d": other.id},
        )
    assert (
        world.api.get(
            chunk_path(
                world.org_id, world.ws, other.id, other_pub.version_id, other_pub.chunk_ids[0]
            ),
            world.t.owner,
        ).status
        == 404
    )


def test_corrupt_or_missing_artifact_is_a_safe_error(rig: Rig, world: World) -> None:
    doc = rig.seed_world(world, title="Report")
    pub = rig.publish(world.org_id, world.ws, doc, TEXTS)
    key = f"org/{world.org_id}/rev/{pub.revision_id}/chunks.json"
    original_bytes = b"".join(rig.artifact_objects.get_object(key))
    rig.artifacts.put(
        key, original_bytes.replace(b"herons", b"HERONS"), content_type="application/json"
    )
    corrupt = world.api.get(evidence_url(world, pub), world.viewer)
    assert corrupt.status == 410
    assert corrupt.body == {"detail": "evidence_unavailable"}
    assert "herons" not in corrupt.raw.text.lower()
    rig.artifact_objects.delete_object(key)
    missing = world.api.get(evidence_url(world, pub), world.viewer)
    assert missing.status == 410
    assert missing.body == {"detail": "evidence_unavailable"}


def test_store_outage_is_503(rig: Rig, world: World) -> None:
    doc = rig.seed_world(world, title="Report")
    pub = rig.publish(world.org_id, world.ws, doc, TEXTS)

    class Down:
        def get_verified(self, *a: object, **k: object) -> bytes:
            raise StorageUnavailable("down")

    original = rig.artifacts
    rig.artifacts = Down()  # type: ignore[assignment]
    try:
        res = world.api.get(evidence_url(world, pub), world.viewer)
    finally:
        rig.artifacts = original
    assert res.status == 503


# --------------------------------------------------------------------------- the resolver


def test_every_citation_resolves_to_the_cited_version(rig: Rig, world: World) -> None:
    doc = rig.seed_world(world, title="Evolving", versions=2)
    v1 = rig.publish(world.org_id, world.ws, doc, TEXTS)
    cited = ref_of(v1, 1)
    resolved = resolve(rig, world, cited)
    assert resolved is not None
    assert resolved.version_number == 1
    assert resolved.is_current_version is True
    v2 = rig.publish(world.org_id, world.ws, doc, ["Brand new version text."], version=2)
    after = resolve(rig, world, cited)  # a newer version is published: still the cited one
    assert after is not None
    assert after.version_number == 1
    assert after.is_current_version is False
    assert after.ref.version_id == v1.version_id
    now = resolve(rig, world, ref_of(v2))
    assert now is not None
    assert now.is_current_version is True
    assert now.title == "Evolving"


@pytest.mark.parametrize("who", ["owner", "viewer"])
def test_citation_scope_checks(rig: Rig, world: World, who: str) -> None:
    other_ws = world.api.create_workspace(world.org_id, world.t.owner)
    doc = rig.seed(world.org_id, other_ws, world.t.owner, title="Elsewhere")
    pub = rig.publish(world.org_id, other_ws, doc, TEXTS)
    result = resolve(rig, world, ref_of(pub))
    if who == "owner":
        assert result is not None  # org owner reads every workspace
    else:
        assert resolve(rig, world, ref_of(pub), who) is None  # the viewer lost/never had it


def test_deleted_forged_and_mismatched_citations_do_not_resolve(rig: Rig, world: World) -> None:
    doc = rig.seed_world(world, title="D")
    pub = rig.publish(world.org_id, world.ws, doc, TEXTS)
    other = rig.seed_world(world, title="O")
    other_pub = rig.publish(world.org_id, world.ws, other, ["other text"])
    good = ref_of(pub)
    assert resolve(rig, world, good, "viewer") is not None
    forged_chunk = CitationRef(uuid.uuid4(), pub.document_id, pub.version_id)
    wrong_doc = CitationRef(pub.chunk_ids[0], other.id, pub.version_id)
    wrong_version = CitationRef(pub.chunk_ids[0], pub.document_id, other_pub.version_id)
    foreign_chunk = CitationRef(other_pub.chunk_ids[0], pub.document_id, pub.version_id)
    for bad in (forged_chunk, wrong_doc, wrong_version, foreign_chunk):
        assert resolve(rig, world, bad) is None
    with rig.admin.begin() as conn:
        conn.execute(
            text("UPDATE documents SET deleted_at = now(), deleted_by = created_by WHERE id = :d"),
            {"d": doc.id},
        )
    assert resolve(rig, world, good) is None  # deleted document: unresolved for everyone
    removed = world.api.delete(
        f"/v1/orgs/{world.org_id}/workspaces/{world.ws}/members/{world.viewer.id}",
        world.t.owner,
    )
    assert removed.status == 204
    with rig.tenant(world.viewer, world.org_id) as session, pytest.raises(EmptyScope):
        resolve_scope(session, org_id=world.org_id, user_id=world.viewer.id)  # nothing left
