"""Dataset, schema and loader validation, plus consistency between the dataset and the corpus."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

import loader
import metrics
import pytest
from corpus_build import EvalEnv, build_environment


@pytest.fixture(scope="module")
def env() -> EvalEnv:
    return build_environment()


@pytest.fixture(scope="module")
def items(env: EvalEnv) -> list[loader.Item]:
    return loader.load_dataset(loader.DEFAULT_DATASET, env.manifest)


def test_dataset_size_and_kind_coverage(items: list[loader.Item]) -> None:
    counts = Counter(i.kind for i in items)
    assert len(items) >= 80
    assert set(counts) == set(loader.KINDS)
    assert counts["answerable"] >= 30
    assert counts["unanswerable"] >= 10
    assert counts["permission"] >= 10
    assert {i.persona for i in items} >= {"viewer-eng", "viewer-hr", "org-admin", "outsider"}


def test_permission_items_cover_cross_workspace_and_cross_org(
    items: list[loader.Item], env: EvalEnv
) -> None:
    perm = [i for i in items if i.kind == "permission"]
    orgs = env.manifest.workspaces
    cross_org = 0
    for item in perm:
        persona = env.manifest.personas[item.persona]
        assert item.forbidden, item.id
        for doc in env.manifest.documents:
            text = " ".join(env.manifest.read(v).decode("utf-8", "ignore") for v in doc.versions)
            if any(metrics.contains(text, f) for f in item.forbidden) and (
                doc.workspace not in persona.workspaces and orgs[doc.workspace] != persona.org
            ):
                cross_org += 1
    assert cross_org >= 5


def test_every_expected_fact_is_in_the_current_text_of_an_expected_doc(
    items: list[loader.Item], env: EvalEnv
) -> None:
    for item in items:
        if not item.expected_facts:
            continue
        text = " ".join(
            c.text
            for c in env.chunks.values()
            if c.state == "published" and c.document_id in item.expected_docs
        )
        for fact in item.expected_facts:
            assert metrics.contains(text, fact), (
                f"{item.id}: fact {fact!r} not in {item.expected_docs}"
            )


def test_every_forbidden_string_exists_somewhere_in_the_corpus(
    items: list[loader.Item], env: EvalEnv
) -> None:
    everything = " ".join(c.text for c in env.chunks.values())
    for item in items:
        for bad in item.forbidden:
            assert metrics.contains(everything, bad), f"{item.id}: {bad!r} is not in any document"


def test_updated_fact_forbidden_strings_are_only_in_the_old_version(
    items: list[loader.Item], env: EvalEnv
) -> None:
    for item in (i for i in items if i.kind == "updated-fact"):
        current = " ".join(
            c.text
            for c in env.chunks.values()
            if c.state == "published" and c.document_id in item.expected_docs
        )
        old = " ".join(c.text for c in env.chunks.values() if c.state == "superseded")
        for bad in item.forbidden:
            assert not metrics.contains(current, bad), item.id
            assert metrics.contains(old, bad), item.id


def test_deleted_doc_items_expect_abstention(items: list[loader.Item], env: EvalEnv) -> None:
    deleted_text = " ".join(c.text for c in env.chunks.values() if c.state == "deleted")
    for item in (i for i in items if i.kind == "deleted-doc"):
        assert item.expect_abstain
        assert any(metrics.contains(deleted_text, f) for f in item.forbidden)


def test_schema_rejects_bad_items() -> None:
    schema = json.loads(loader.SCHEMA_PATH.read_text(encoding="utf-8"))
    good = {
        "id": "x",
        "question": "What?",
        "kind": "answerable",
        "persona": "p",
        "expected_docs": [],
        "expected_facts": ["a"],
        "forbidden": [],
        "expect_abstain": False,
    }
    assert loader.validate_schema(good, schema) == []
    assert loader.validate_schema({**good, "kind": "weird"}, schema)
    assert loader.validate_schema({**good, "extra": 1}, schema)
    assert loader.validate_schema({k: v for k, v in good.items() if k != "persona"}, schema)
    assert loader.validate_schema({**good, "expect_abstain": "no"}, schema)
    assert loader.validate_schema({**good, "expected_docs": [3]}, schema)


def _write_dataset(tmp_path: Path, *lines: dict[str, object]) -> Path:
    path = tmp_path / "d.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


def test_loader_rejects_semantic_errors(tmp_path: Path, env: EvalEnv) -> None:
    base = {
        "id": "x",
        "question": "What is it?",
        "kind": "answerable",
        "persona": "viewer-eng",
        "expected_docs": ["eng-staging"],
        "expected_facts": ["kestrel"],
        "forbidden": [],
        "expect_abstain": False,
    }
    ok = loader.load_dataset(_write_dataset(tmp_path, base), env.manifest)
    assert ok[0].expected_docs == ("eng-staging",)
    # titles resolve to ids
    titled = {**base, "expected_docs": ["Staging cluster"]}
    assert loader.load_dataset(_write_dataset(tmp_path, titled), env.manifest)[0].expected_docs == (
        "eng-staging",
    )
    for bad in (
        {**base, "persona": "ghost"},
        {**base, "expected_docs": ["no-such-doc"]},
        {**base, "expect_abstain": True},
        {**base, "expected_facts": []},
    ):
        with pytest.raises(loader.DatasetError):
            loader.load_dataset(_write_dataset(tmp_path, bad), env.manifest)
    with pytest.raises(loader.DatasetError, match="duplicate id"):
        loader.load_dataset(_write_dataset(tmp_path, base, base), env.manifest)


def test_loader_loads_a_user_corpus_directory_with_the_same_layout(tmp_path: Path) -> None:
    user = tmp_path / "user-corpus"
    shutil.copytree(loader.DEFAULT_CORPUS, user)
    manifest = loader.load_manifest(user)
    assert len(manifest.documents) >= 25
    env = build_environment(user)
    assert env.chunk_count("published") > 0


def test_manifest_errors(tmp_path: Path) -> None:
    user = tmp_path / "c"
    shutil.copytree(loader.DEFAULT_CORPUS, user)
    raw = json.loads((user / "manifest.json").read_text(encoding="utf-8"))
    raw["documents"][0]["versions"][0]["file"] = "missing.md"
    (user / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(loader.DatasetError, match="missing file"):
        loader.load_manifest(user)
    with pytest.raises(loader.DatasetError, match="cannot read"):
        loader.load_manifest(tmp_path / "nothing")
