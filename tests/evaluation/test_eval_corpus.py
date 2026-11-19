"""The seed corpus builds deterministically through the real extraction and chunking code."""

from __future__ import annotations

from pathlib import Path

import loader
from corpus_build import build_environment
from pdfwriter import PDF_SOURCES, build_pdf
from run_eval import corpus_sha256


def test_build_is_deterministic() -> None:
    a, b = build_environment(), build_environment()
    assert a.chunks == b.chunks
    assert corpus_sha256(a) == corpus_sha256(b)
    assert a.space_id == b.space_id


def test_seed_corpus_has_the_required_shape() -> None:
    env = build_environment()
    manifest = env.manifest
    assert len(manifest.orgs) == 2
    assert len(manifest.workspaces) == 4
    assert len(manifest.documents) >= 25
    types = {d.content_type for d in manifest.documents}
    assert types == {"application/pdf", "text/plain", "text/markdown", "text/html"}
    assert any(len(d.versions) > 1 for d in manifest.documents)
    assert any(d.deleted for d in manifest.documents)
    assert any(d.hostile for d in manifest.documents)
    by_doc: dict[str, int] = {}
    for c in env.chunks.values():
        by_doc[c.document_id] = by_doc.get(c.document_id, 0) + 1
    assert by_doc["eng-staging"] == 1
    assert by_doc["eng-architecture"] >= 8  # the very long document spans many chunks


def test_states_match_the_pipeline_semantics() -> None:
    env = build_environment()
    states = {(c.document_id, c.version): c.state for c in env.chunks.values()}
    assert states[("eng-deploy", 1)] == "superseded"
    assert states[("eng-deploy", 2)] == "published"
    assert states[("sales-pricing", 1)] == "superseded"
    assert states[("eng-legacy-vpn", 1)] == "deleted"
    # the deleted document is purged and superseded chunks are unpublished: neither is searchable
    from app.modules.knowledge.ports import SearchFilters

    scope = env.scopes["org-a"]
    filters = SearchFilters(
        frozenset(env.workspace_uuid[w] for w in env.manifest.personas["org-admin"].workspaces)
    )
    for query in (
        "vpn-old.example.internal passphrase",
        "deploy freeze Friday 14:00 canary 10 percent",
    ):
        hits = env.repo.search_keyword(scope, query, filters, limit=50)
        assert all(str(h.chunk_id) in env.chunks for h in hits)
        assert all(env.chunks[str(h.chunk_id)].state == "published" for h in hits)
    assert env.chunk_count("published") > 0


def test_pdfs_are_reproducible_and_extract() -> None:
    files = loader.DEFAULT_CORPUS / "files"
    for name, pages in PDF_SOURCES.items():
        assert (files / name).read_bytes() == build_pdf(pages)
    env = build_environment()
    text = " ".join(c.text for c in env.chunks.values() if c.document_id == "eng-security")
    assert "14 characters" in text


def test_multilingual_text_survives_extraction() -> None:
    env = build_environment()
    text = " ".join(c.text for c in env.chunks.values() if c.document_id == "hr-reise-de")
    assert "Verpflegungspauschale" in text
    assert "21 días" in text


def test_user_corpus_copy_builds_identically(tmp_path: Path) -> None:
    import shutil

    copy = tmp_path / "copy"
    shutil.copytree(loader.DEFAULT_CORPUS, copy)
    assert build_environment(copy).chunks == build_environment().chunks
