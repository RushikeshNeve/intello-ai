"""M03.03.02 / M03.03.03 acceptance and deletion: every test runs on the in-memory index AND on
the real RushDB 3.0.10 (the ``env`` fixture is parametrized)."""

from __future__ import annotations

from typing import Any

import pytest
from pipe_support import Env, SimulatedCrash, document_text, eventually
from sqlalchemy import text

from app.modules.ingestion import pipeline as pipeline_mod
from app.modules.ingestion.artifacts import artifact_key
from app.modules.ingestion.runner_types import LeaseLost
from app.modules.knowledge.deletion import DefaultDocumentDeletedHandler, purge_document
from app.modules.knowledge.publication import (
    commit_pointer,
    is_revision_published,
    publish_revision,
    reconcile_document,
)


def crash_on(env: Env, method: str, *, after_calls: int = 0) -> None:
    seen = {"n": 0}

    def boom(*_a: Any, **_k: Any) -> None:
        seen["n"] += 1
        if seen["n"] > after_calls:
            raise SimulatedCrash

    env.repo.before[method] = boom


def published_in_index(env: Env, scope: Any, revision_id: Any) -> int:
    return int(env.repo.count_chunks(env.scope_of(scope.org_id), revision_id, published=True))


def live(env: Env, scope: Any, revision_id: Any) -> bool:
    with env.sessions(scope.org_id) as s:
        return is_revision_published(s, scope.org_id, scope.document_id, revision_id)


# --------------------------------------------------------------- M03.03.02: versioned indexing


def test_old_and_new_revisions_remain_isolated(env: Env) -> None:
    scope = env.new_scope()
    v2 = env.job(scope, env.version(scope, document_text("alphaword", 8)))
    env.runner().run(v2, env.ctx(v2))
    assert eventually(lambda: env.hits_revisions(scope, "alphaword") == {v2.revision_id})

    # v3 is fully indexed (unpublished) while v2 is the published revision.
    v3 = env.job(scope, env.version(scope, document_text("alphaword betaword", 8)))
    crash_on(env, "list_chunk_ids")  # dies in ``validate``: indexed, not ready, not published
    with pytest.raises(SimulatedCrash):
        env.runner().run(v3, env.ctx(v3))
    sc = env.scope_of(scope.org_id)
    assert env.repo.count_chunks(sc, v3.revision_id) > 0  # both revisions coexist in the index
    assert env.repo.count_chunks(sc, v3.revision_id, published=True) == 0
    assert env.hits_revisions(scope, "alphaword") == {v2.revision_id}  # v3 is invisible
    assert env.hits_revisions(scope, "betaword") == set()
    assert env.pointer(scope) == v2.revision_id

    env.repo.before.clear()
    env.runner().run(v3, env.ctx(v3))  # the barrier flips v2 -> v3
    assert env.pointer(scope) == v3.revision_id
    assert eventually(lambda: env.hits_revisions(scope, "betaword") == {v3.revision_id})
    assert env.hits_revisions(scope, "alphaword") == {v3.revision_id}  # never both
    assert not live(env, scope, v2.revision_id)
    assert live(env, scope, v3.revision_id)
    # chunk ids and counts of v2 are untouched by v3 (separate revision_id)
    assert env.repo.count_chunks(sc, v2.revision_id) > 0
    ids2 = set(env.repo.list_chunk_ids(sc, v2.revision_id))
    ids3 = set(env.repo.list_chunk_ids(sc, v3.revision_id))
    assert ids2.isdisjoint(ids3)


def test_reindex_of_the_same_revision_is_idempotent(env: Env) -> None:
    scope = env.new_scope()
    claim = env.job(scope, env.version(scope, document_text("idemword")))
    env.runner().run(claim, env.ctx(claim))
    sc = env.scope_of(scope.org_id)
    before = env.repo.list_chunk_ids(sc, claim.revision_id)
    # Re-run the index step directly twice more: same ids, same count, still published.
    from app.modules.ingestion import artifacts as art
    from app.modules.knowledge import indexing
    from app.modules.knowledge.indexing import RevisionIdentity

    chunks = art.decode_chunks(
        b"".join(
            env.artifacts_raw.get_object(artifact_key(scope.org_id, claim.revision_id, "chunk"))
        )
    )
    vectors = art.decode_embeddings(
        b"".join(
            env.artifacts_raw.get_object(artifact_key(scope.org_id, claim.revision_id, "embed"))
        ),
        len(chunks),
        32,
    )
    ident = RevisionIdentity(
        scope.org_id, scope.workspace_id, scope.document_id, claim.version_id,
        claim.revision_id, "1", env.embedder.spec.space_id,
    )  # fmt: skip
    for _ in range(2):
        indexing.index_chunks(env.repo, sc, ident, chunks, vectors, dimensions=32, slice_size=3)
    assert env.repo.list_chunk_ids(sc, claim.revision_id) == before
    assert env.repo.count_chunks(sc, claim.revision_id, published=True) == len(before)


# ------------------------------------------------------------ M03.03.03: publication barrier


def test_partial_index_is_never_visible(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_mod, "_CHUNK_SLICE", 3)
    scope = env.new_scope()
    claim = env.job(scope, env.version(scope, document_text("partialword", 10)))
    crash_on(env, "upsert_chunks", after_calls=2)  # dies after 2 slices (6 chunks) were uploaded
    with pytest.raises(SimulatedCrash):
        env.runner().run(claim, env.ctx(claim))
    sc = env.scope_of(scope.org_id)
    assert env.repo.count_chunks(sc, claim.revision_id) == 6
    assert env.repo.count_chunks(sc, claim.revision_id, published=True) == 0
    assert env.hits_revisions(scope, "partialword") == set()  # real adapter: nothing visible
    assert env.pointer(scope) is None
    assert env.rev_state(claim.revision_id) == "building"
    assert not live(env, scope, claim.revision_id)

    env.repo.before.clear()  # the retry completes the same revision
    env.runner().run(claim, env.ctx(claim))
    assert env.repo.count_chunks(sc, claim.revision_id) == env.repo.count_chunks(
        sc, claim.revision_id, published=True
    )
    assert eventually(lambda: env.hits_revisions(scope, "partialword") == {claim.revision_id})


def test_crash_between_pg_commit_and_flip_is_repaired_by_reconcile(env: Env) -> None:
    scope = env.new_scope()
    claim = env.job(scope, env.version(scope, document_text("flipword")))
    crash_on(env, "set_published")
    with pytest.raises(SimulatedCrash):
        env.runner().run(claim, env.ctx(claim))
    env.repo.before.clear()
    # The pointer committed (the publication) but the index flags did not move yet.
    assert env.pointer(scope) == claim.revision_id
    assert env.rev_state(claim.revision_id) == "published"
    assert live(env, scope, claim.revision_id)
    assert published_in_index(env, scope, claim.revision_id) == 0
    assert env.hits_revisions(scope, "flipword") == set()  # safe lag: not visible yet

    result = reconcile_document(env.sessions, env.repo, scope.org_id, scope.document_id)
    assert result.action == "repaired"
    assert result.flipped > 0
    assert eventually(lambda: env.hits_revisions(scope, "flipword") == {claim.revision_id})
    again = reconcile_document(env.sessions, env.repo, scope.org_id, scope.document_id)
    assert again.action == "ok"  # idempotent


def test_flip_failure_is_retried_by_the_job(env: Env) -> None:
    from app.modules.ingestion.runner_types import RetryableJobError
    from app.modules.knowledge.ports import KnowledgeStoreUnavailable

    scope = env.new_scope()
    claim = env.job(scope, env.version(scope, document_text("retryflip")))

    def down(*_a: Any, **_k: Any) -> None:
        raise KnowledgeStoreUnavailable("down")

    env.repo.before["set_published"] = down
    with pytest.raises(RetryableJobError):
        env.runner().run(claim, env.ctx(claim))
    env.repo.before.clear()
    assert env.pointer(scope) == claim.revision_id
    env.runner().run(claim, env.ctx(claim))  # the retry only redoes the flip
    assert eventually(lambda: env.hits_revisions(scope, "retryflip") == {claim.revision_id})
    n_audit = env.q(
        "SELECT count(*) FROM audit_events WHERE event_type = 'document.version.indexed'"
        " AND target_id = :d",
        d=str(scope.document_id),
    )[0][0]
    n_outbox = env.q(
        "SELECT count(*) FROM outbox_events WHERE event_type = 'knowledge.revision.published'"
        " AND aggregate_id = :d",
        d=scope.document_id,
    )[0][0]
    assert (n_audit, n_outbox) == (1, 1)  # published exactly once


def test_older_version_finishing_late_never_regresses_the_pointer(env: Env) -> None:
    scope = env.new_scope()
    older = env.version(scope, document_text("oldword"))
    newer = env.version(scope, document_text("newword"))
    c_new = env.job(scope, newer)
    c_old = env.job(scope, older)
    env.runner().run(c_new, env.ctx(c_new))
    result = env.runner().run(c_old, env.ctx(c_old))
    assert result.detail == "superseded"
    assert env.pointer(scope) == c_new.revision_id
    assert env.rev_state(c_old.revision_id) == "superseded"
    assert env.hits_revisions(scope, "oldword") == set()


def test_stale_worker_cannot_publish(env: Env) -> None:
    other = env.new_scope()
    c2 = env.job(other, env.version(other, document_text("staleword2")))
    ctx2 = env.ctx(c2)
    # The lease is taken over (epoch bumped) before the worker reaches the barrier.
    with env.admin.begin() as conn:
        conn.execute(
            text("UPDATE ingestion_jobs SET lease_epoch = 2 WHERE id = :j"), {"j": c2.job_id}
        )
    with pytest.raises(LeaseLost):
        env.runner().run(c2, ctx2)
    assert env.pointer(other) is None
    # And the barrier itself refuses the stale claim.
    with pytest.raises(LeaseLost), env.sessions(other.org_id) as session:
        commit_pointer(session, c2)
    with pytest.raises(LeaseLost):
        publish_revision(env.sessions, env.repo, c2)
    assert env.pointer(other) is None


def test_reconcile_purges_superseded_and_requeues_missing_index(env: Env) -> None:
    scope = env.new_scope()
    v2 = env.job(scope, env.version(scope, document_text("purgeword")))
    v3 = env.job(scope, env.version(scope, document_text("purgeword")))
    env.runner().run(v2, env.ctx(v2))
    env.runner().run(v3, env.ctx(v3))
    sc = env.scope_of(scope.org_id)
    assert env.repo.count_chunks(sc, v2.revision_id) > 0  # superseded but still stored
    result = reconcile_document(
        env.sessions, env.repo, scope.org_id, scope.document_id, artifacts=env.artifacts
    )
    assert result.purged_revisions == 1
    assert env.repo.count_chunks(sc, v2.revision_id) == 0
    assert env.rev_state(v2.revision_id) == "purged"
    assert (
        env.artifacts_raw.head_object(artifact_key(scope.org_id, v2.revision_id, "embed")) is None
    )

    with env.admin.begin() as conn:  # the worker finished the job (state is the worker's)
        conn.execute(
            text("UPDATE ingestion_jobs SET state = 'succeeded', lease_owner = NULL WHERE id = :j"),
            {"j": v3.job_id},
        )
    env.repo.delete_revision(sc, v3.revision_id)  # the index lost the pointed revision
    result = reconcile_document(env.sessions, env.repo, scope.org_id, scope.document_id)
    assert result.action == "reindex_requested"
    rows = env.q("SELECT state FROM ingestion_jobs WHERE id = :j", j=v3.job_id)
    assert rows[0][0] == "queued"
    assert (
        env.q(
            "SELECT count(*) FROM outbox_events WHERE aggregate_id = :j AND dispatched_at IS NULL",
            j=v3.job_id,
        )[0][0]
        >= 1
    )


# ------------------------------------------------------------------------------- deletion


def test_delete_after_publish_denies_in_pg_before_purging_the_index(env: Env) -> None:
    scope = env.new_scope()
    claim = env.job(scope, env.version(scope, document_text("doomedword")))
    env.runner().run(claim, env.ctx(claim))
    assert eventually(lambda: env.hits_revisions(scope, "doomedword") == {claim.revision_id})
    with env.admin.begin() as conn:  # the M02 delete endpoint's transaction
        conn.execute(
            text("UPDATE documents SET deleted_at = now(), deleted_by = :u WHERE id = :d"),
            {"u": scope.owner_id, "d": scope.document_id},
        )

    seen: dict[str, Any] = {}

    def at_purge(*_a: Any, **_k: Any) -> None:  # runs right before RushDB's delete_document
        seen["live"] = live(env, scope, claim.revision_id)
        seen["state"] = env.rev_state(claim.revision_id)

    env.repo.before["delete_document"] = at_purge
    result = purge_document(env.sessions, env.repo, env.artifacts, scope.org_id, scope.document_id)
    assert seen == {"live": False, "state": "purged"}  # PostgreSQL denied first
    assert not result.skipped
    assert result.cancelled_jobs == 1
    assert env.hits_revisions(scope, "doomedword") == set()
    assert env.repo.count_chunks(env.scope_of(scope.org_id), claim.revision_id) == 0
    assert (
        env.artifacts_raw.head_object(artifact_key(scope.org_id, claim.revision_id, "chunk"))
        is None
    )
    assert (
        env.q("SELECT state FROM ingestion_jobs WHERE id = :j", j=claim.job_id)[0][0] == "cancelled"
    )
    again = purge_document(env.sessions, env.repo, env.artifacts, scope.org_id, scope.document_id)
    assert again.purged_revisions == 0  # idempotent
    handler = DefaultDocumentDeletedHandler(
        sessions=env.sessions, repo=env.repo, artifacts=env.artifacts
    )
    handler.handle(scope.org_id, scope.document_id)


def test_deletion_event_for_a_live_document_is_ignored(env: Env) -> None:
    scope = env.new_scope()
    claim = env.job(scope, env.version(scope, document_text("liveword")))
    env.runner().run(claim, env.ctx(claim))
    result = purge_document(env.sessions, env.repo, env.artifacts, scope.org_id, scope.document_id)
    assert result.skipped
    assert env.pointer(scope) == claim.revision_id
    assert live(env, scope, claim.revision_id)
    assert eventually(lambda: env.hits_revisions(scope, "liveword") == {claim.revision_id})
