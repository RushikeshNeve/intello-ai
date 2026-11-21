"""M12.05 acceptance: blue/green re-embedding with pointer-authoritative cutover and rollback."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from pipe_support import SPEC, SPEC_B, Env, SimulatedCrash, document_text
from sqlalchemy import Engine, text

from app.core.db import tenant_session
from app.modules.ai.embeddings import EmbeddingUnavailable, FakeEmbeddingClient
from app.modules.ingestion.runner_types import LeaseLost
from app.modules.knowledge.publication import reconcile_document
from app.modules.knowledge.reindex import service

MakeEnv = Callable[..., Env]


def publish_one(env: Env) -> tuple[Any, uuid.UUID]:
    """A published document in the old space; returns (scope, old revision id)."""
    scope = env.new_scope()
    version = env.version(scope, document_text("zebrafishone"))
    claim = env.job(scope, version)
    assert env.runner().run(claim, env.ctx(claim)).detail == "published"
    return scope, claim.revision_id


def plan(app: Engine, scope: Any) -> uuid.UUID:
    with tenant_session(user_id=scope.owner_id, org_id=scope.org_id, engine=app) as s:
        return service.create_job(
            s,
            scope.org_id,
            to_space=SPEC_B.space_id,
            to_dimensions=SPEC_B.dimensions,
            default_space=SPEC.space_id,
            requested_by=scope.owner_id,
        )


def run(env: Env, scope: Any, job: uuid.UUID, embedder: Any = None, owner: str = "w1") -> Any:
    return service.run_job(
        env.sessions,
        env.repo,
        env.artifacts,
        embedder or FakeEmbeddingClient(SPEC_B),
        scope.org_id,
        job,
        owner,
    )


def flip(app: Engine, user: Any, org: Any, fn: str, job: uuid.UUID) -> str:
    with tenant_session(user_id=user, org_id=org, engine=app) as s:
        return str(s.execute(text(f"SELECT {fn}(:j)"), {"j": job}).scalar_one())


def job_state(env: Env, job: uuid.UUID) -> Any:
    return env.q(
        "SELECT state, total, done, failed, lease_epoch FROM reindex_jobs WHERE id=:j", j=job
    )[0]


def align(env: Env, scope: Any) -> None:
    reconcile_document(env.sessions, env.repo, scope.org_id, scope.document_id)


def active_space(env: Env, scope: Any) -> str | None:
    rows = env.q("SELECT space_id FROM org_active_spaces WHERE org_id=:o", o=scope.org_id)
    return rows[0][0] if rows else None


def test_reindex_cutover_rollback_and_purge(make_env: MakeEnv, app_engine: Engine) -> None:
    env = make_env()
    scope, old_rev = publish_one(env)
    job = plan(app_engine, scope)
    assert service.claim_job(env.sessions, scope.org_id, uuid.uuid4(), "x") is None  # unknown job

    result = run(env, scope, job)
    assert (result.state, result.total, result.done, result.failed) == ("ready", 1, 1, 0)
    new_rev = env.q(
        "SELECT id FROM knowledge_revisions WHERE embedding_space_id=:s", s=SPEC_B.space_id
    )[0][0]

    # Until cutover search and the pointer stay on the old space; the new one is built, unpublished.
    assert env.pointer(scope) == old_rev
    assert env.rev_state(new_rev) == "ready"
    assert env.hits_revisions(scope, "zebrafishone") == {old_rev}
    assert active_space(env, scope) is None

    assert flip(app_engine, scope.owner_id, scope.org_id, "reindex_cutover", job) == "ok"
    assert env.pointer(scope) == new_rev
    assert (env.rev_state(new_rev), env.rev_state(old_rev)) == ("published", "ready")
    assert active_space(env, scope) == SPEC_B.space_id
    assert env.q("SELECT 1 FROM outbox_events WHERE event_type='knowledge.reconcile'")
    align(env, scope)
    assert env.hits_revisions(scope, "zebrafishone") == {new_rev}
    assert flip(app_engine, scope.owner_id, scope.org_id, "reindex_cutover", job) == "bad_state"

    assert flip(app_engine, scope.owner_id, scope.org_id, "reindex_rollback", job) == "ok"
    assert env.pointer(scope) == old_rev
    assert active_space(env, scope) == SPEC.space_id
    align(env, scope)
    assert env.hits_revisions(scope, "zebrafishone") == {old_rev}

    # The abandoned space goes only on the explicit command, and then rollback is over.
    assert service.purge_old_space(env.sessions, env.repo, env.artifacts, scope.org_id, job) == 1
    assert env.rev_state(new_rev) == "purged"
    assert env.repo.count_chunks(env.scope_of(scope.org_id), new_rev) == 0
    assert env.rev_state(old_rev) == "published"


def test_crash_resume_and_fencing(make_env: MakeEnv, app_engine: Engine) -> None:
    env = make_env()
    scope, _ = publish_one(env)
    job = plan(app_engine, scope)

    def boom(*_a: Any, **_k: Any) -> None:
        raise SimulatedCrash

    env.repo.before["ensure_indexes"] = boom
    with pytest.raises(SimulatedCrash):
        run(env, scope, job)
    del env.repo.before["ensure_indexes"]
    assert job_state(env, job)[0] == "running"
    assert (
        run(env, scope, job, owner="w2") is None
    )  # lease still held: a duplicate delivery is a no-op

    stale = service.Claim(
        job, scope.org_id, "w1", 1, SPEC.space_id, SPEC_B.space_id, SPEC_B.dimensions
    )
    with env.admin.begin() as conn:
        conn.execute(
            text("UPDATE reindex_jobs SET lease_expires_at = now() - interval '1 s' WHERE id = :j"),
            {"j": job},
        )
    result = run(env, scope, job, owner="w2")
    assert result is not None
    assert result.state == "ready"
    assert job_state(env, job)[4] == 2  # new fencing epoch
    mirrored = env.q(
        "SELECT 1 FROM knowledge_revisions WHERE embedding_space_id=:s AND org_id=:o",
        s=SPEC_B.space_id,
        o=scope.org_id,
    )
    assert len(mirrored) == 1
    with pytest.raises(LeaseLost):  # the revived first worker is fenced out
        service._fence(env.sessions, stale, 60)


def test_embedding_failure_fails_job_then_resumes(make_env: MakeEnv, app_engine: Engine) -> None:
    env = make_env()
    scope, old_rev = publish_one(env)
    job = plan(app_engine, scope)
    bad = FakeEmbeddingClient(SPEC_B)
    bad.fail_next(EmbeddingUnavailable("down"), times=50)
    failed = run(env, scope, job, bad)
    assert failed is not None
    assert (failed.state, failed.failed) == ("failed", 1)
    assert flip(app_engine, scope.owner_id, scope.org_id, "reindex_cutover", job) == "bad_state"
    assert env.pointer(scope) == old_rev  # nothing changed for readers
    assert run(env, scope, job).state == "ready"  # resumable from failed with a healthy embedder


def test_tenant_isolation(make_env: MakeEnv, app_engine: Engine) -> None:
    env = make_env()
    a, _ = publish_one(env)
    b = env.new_scope()
    job = plan(app_engine, a)
    run(env, a, job)
    with tenant_session(user_id=b.owner_id, org_id=b.org_id, engine=app_engine) as s:
        assert service.get_job(s, b.org_id, job) is None
    assert flip(app_engine, b.owner_id, b.org_id, "reindex_cutover", job) == "not_found"
    assert service.claim_job(env.sessions, b.org_id, job, "evil") is None
    assert job_state(env, job)[0] == "ready"
