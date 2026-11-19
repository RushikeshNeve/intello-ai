"""Constraints, composite FKs, indexes and the append-only event log (admin role, M03.01.01)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from m01_seed import Tenant
from m03_support import (
    Scope,
    add_version,
    constraint,
    insert_job,
    insert_outbox,
    insert_revision,
    new_space,
    seed_scope,
    sqlstate,
)
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError


@pytest.fixture
def scope(admin_engine: Engine, migrated: None) -> Scope:
    return seed_scope(admin_engine)


@pytest.fixture
def other(admin_engine: Engine, migrated: None) -> Scope:
    return seed_scope(admin_engine)


def fails(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> DBAPIError:
    with pytest.raises(DBAPIError) as exc, engine.begin() as conn:
        conn.execute(text(sql), params or {})
    return exc.value


def update_job(engine: Engine, job: uuid.UUID, set_clause: str, **params: Any) -> DBAPIError:
    return fails(
        engine,
        f"UPDATE ingestion_jobs SET {set_clause} WHERE id = :j",
        {"j": job, **params},
    )


# -- ingestion_jobs ---------------------------------------------------------------------------


def test_job_defaults(admin_engine: Engine, scope: Scope) -> None:
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope)
        row = (
            conn.execute(text("SELECT * FROM ingestion_jobs WHERE id = :j"), {"j": job})
            .mappings()
            .one()
        )
    assert row["state"] == "queued"
    assert (row["attempts"], row["lease_epoch"], row["max_attempts"]) == (0, 0, 5)
    assert row["lease_owner"] is None
    assert row["created_at"] is not None


@pytest.mark.parametrize(
    ("set_clause", "name"),
    [
        ("state = 'bogus'", "ck_ingestion_jobs_state"),
        ("attempts = -1", "ck_ingestion_jobs_attempts"),
        ("max_attempts = 0", "ck_ingestion_jobs_max_attempts"),
        ("lease_epoch = -1", "ck_ingestion_jobs_lease_epoch"),
        ("current_stage = 'transcode'", "ck_ingestion_jobs_current_stage"),
        ("last_error_detail = repeat('x', 501)", "ck_ingestion_jobs_last_error_detail"),
        # lease consistency: running <=> lease_owner
        ("state = 'running'", "ck_ingestion_jobs_lease"),
        ("lease_owner = 'w1'", "ck_ingestion_jobs_lease"),
    ],
)
def test_job_checks(admin_engine: Engine, scope: Scope, set_clause: str, name: str) -> None:
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope)
    err = update_job(admin_engine, job, set_clause)
    assert (sqlstate(err), constraint(err)) == ("23514", name)


def test_job_lease_consistency_accepts_both_valid_shapes(
    admin_engine: Engine, scope: Scope
) -> None:
    with admin_engine.begin() as conn:
        running = insert_job(conn, scope, state="running", lease_owner="w1", lease_expires_in=60)
        queued = insert_job(conn, scope, state="queued")
        # leaving running requires dropping the owner in the same statement
        conn.execute(
            text("UPDATE ingestion_jobs SET state = 'succeeded', lease_owner = NULL WHERE id = :j"),
            {"j": running},
        )
        assert (
            conn.execute(
                text("SELECT state FROM ingestion_jobs WHERE id = :j"), {"j": queued}
            ).scalar_one()
            == "queued"
        )


@pytest.mark.parametrize(
    "state", ["queued", "running", "succeeded", "dead", "unsupported", "cancelled"]
)
def test_every_documented_job_state_is_accepted(
    admin_engine: Engine, scope: Scope, state: str
) -> None:
    owner = "w1" if state == "running" else None
    with admin_engine.begin() as conn:
        insert_job(conn, scope, state=state, lease_owner=owner)


def test_revision_id_is_unique_per_job(admin_engine: Engine, scope: Scope) -> None:
    space = new_space()
    with admin_engine.begin() as conn:
        insert_job(conn, scope, space=space)
    with pytest.raises(DBAPIError) as exc, admin_engine.begin() as conn:
        insert_job(conn, scope, space=space)
    assert (sqlstate(exc.value), constraint(exc.value)) == (
        "23505",
        "uq_ingestion_jobs_revision_id",
    )


def test_job_cannot_name_another_orgs_workspace(
    admin_engine: Engine, scope: Scope, other: Scope
) -> None:
    """org A's id with org B's workspace: the (workspace_id, org_id) composite FK rejects it."""
    mixed = Scope(
        Tenant(scope.org_id, scope.owner_id, other.workspace_id),
        other.document_id,
        other.version_id,
    )
    with pytest.raises(DBAPIError) as exc, admin_engine.begin() as conn:
        insert_job(conn, mixed)
    assert (sqlstate(exc.value), constraint(exc.value)) == ("23503", "fk_ingestion_jobs_workspace")


def test_job_cannot_name_a_document_of_another_workspace(
    admin_engine: Engine, scope: Scope, other: Scope
) -> None:
    mixed = Scope(scope.tenant, other.document_id, other.version_id)
    with pytest.raises(DBAPIError) as exc, admin_engine.begin() as conn:
        insert_job(conn, mixed)
    assert (sqlstate(exc.value), constraint(exc.value)) == ("23503", "fk_ingestion_jobs_document")


def test_job_version_must_belong_to_the_document(
    admin_engine: Engine, scope: Scope, other: Scope
) -> None:
    with pytest.raises(DBAPIError) as exc, admin_engine.begin() as conn:
        insert_job(conn, scope, version_id=other.version_id)
    assert (sqlstate(exc.value), constraint(exc.value)) == ("23503", "fk_ingestion_jobs_version")


def test_job_indexes(admin_engine: Engine, migrated: None) -> None:
    with admin_engine.connect() as conn:
        defs: dict[str, str] = {
            r.indexname: r.indexdef
            for r in conn.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
            )
        }
    assert "(state, run_after)" in defs["ix_ingestion_jobs_state_run_after"]
    outbox = defs["ix_outbox_events_undispatched"]
    assert "(available_at, seq)" in outbox
    assert "WHERE (dispatched_at IS NULL)" in outbox


# -- ingestion_job_events ---------------------------------------------------------------------

EVENT_INSERT = (
    "INSERT INTO ingestion_job_events (job_id, org_id, workspace_id, document_id, version_id,"
    " event_type, stage, attempt) VALUES (:j, :o, :w, :d, :v, :t, :s, :a)"
)


def event_params(scope: Scope, job: uuid.UUID, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "j": job,
        "o": scope.org_id,
        "w": scope.workspace_id,
        "d": scope.document_id,
        "v": scope.version_id,
        "t": "queued",
        "s": None,
        "a": 0,
    }
    base.update(over)
    return base


@pytest.mark.parametrize(
    "event_type",
    [
        "queued", "started", "stage_started", "stage_completed", "retry_scheduled",
        "succeeded", "dead", "unsupported", "cancelled", "published",
    ],
)  # fmt: skip
def test_every_documented_event_type_is_accepted(
    admin_engine: Engine, scope: Scope, event_type: str
) -> None:
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope)
        conn.execute(text(EVENT_INSERT), event_params(scope, job, t=event_type))


@pytest.mark.parametrize(
    ("over", "name"),
    [
        ({"t": "exploded"}, "ck_ingestion_job_events_type"),
        ({"s": "transcode"}, "ck_ingestion_job_events_stage"),
        ({"a": -1}, "ck_ingestion_job_events_attempt"),
    ],
)
def test_event_checks(admin_engine: Engine, scope: Scope, over: dict[str, Any], name: str) -> None:
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope)
    err = fails(admin_engine, EVENT_INSERT, event_params(scope, job, **over))
    assert (sqlstate(err), constraint(err)) == ("23514", name)


def test_event_cannot_attach_a_job_to_another_org(
    admin_engine: Engine, scope: Scope, other: Scope
) -> None:
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope)
    # org_id of the other org but the job of this one: (job_id, org_id) composite FK.
    err = fails(
        admin_engine,
        EVENT_INSERT,
        event_params(other, job),
    )
    assert (sqlstate(err), constraint(err)) == ("23503", "fk_ingestion_job_events_job")


def test_event_seq_is_a_generated_identity(admin_engine: Engine, scope: Scope) -> None:
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope)
        for _ in range(3):
            conn.execute(text(EVENT_INSERT), event_params(scope, job))
        seqs = list(
            conn.execute(
                text("SELECT seq FROM ingestion_job_events WHERE job_id = :j ORDER BY seq"),
                {"j": job},
            ).scalars()
        )
    assert len(seqs) == 3
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 3
    # GENERATED ALWAYS: an explicit value is refused.
    err = fails(
        admin_engine,
        "INSERT INTO ingestion_job_events (seq, job_id, org_id, workspace_id, document_id,"
        " version_id, event_type) VALUES (1, :j, :o, :w, :d, :v, 'queued')",
        {k: v for k, v in event_params(scope, job).items() if k not in ("t", "s", "a")},
    )
    assert sqlstate(err) == "428C9"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ingestion_job_events SET detail = '{\"x\": 1}'::jsonb WHERE job_id = :j",
        "UPDATE ingestion_job_events SET event_type = 'dead' WHERE job_id = :j",
        "DELETE FROM ingestion_job_events WHERE job_id = :j",
        "TRUNCATE ingestion_job_events",
    ],
)
def test_events_are_append_only_even_for_the_owner(
    admin_engine: Engine, scope: Scope, statement: str
) -> None:
    with admin_engine.begin() as conn:
        job = insert_job(conn, scope)
        conn.execute(text(EVENT_INSERT), event_params(scope, job))
    err = fails(admin_engine, statement, {"j": job})
    assert sqlstate(err) == "42501"
    assert "append-only" in str(err.orig)
    with admin_engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM ingestion_job_events WHERE job_id = :j"), {"j": job}
        ).scalar_one()
    assert n == 1


# -- knowledge_revisions / ingestion_checkpoints / document_publications ------------------------


def test_revision_unique_per_version_pipeline_space(admin_engine: Engine, scope: Scope) -> None:
    params = {
        "o": scope.org_id,
        "w": scope.workspace_id,
        "d": scope.document_id,
        "v": scope.version_id,
    }
    sql = (
        "INSERT INTO knowledge_revisions (id, org_id, workspace_id, document_id, version_id,"
        " pipeline_version, embedding_space_id) VALUES (:id, :o, :w, :d, :v, '1', 'espace')"
    )
    with admin_engine.begin() as conn:
        conn.execute(text(sql), {**params, "id": uuid.uuid4()})
    err = fails(admin_engine, sql, {**params, "id": uuid.uuid4()})
    assert (sqlstate(err), constraint(err)) == (
        "23505",
        "uq_knowledge_revisions_version_pipeline_space",
    )


@pytest.mark.parametrize(
    ("set_clause", "name"),
    [
        ("state = 'half-baked'", "ck_knowledge_revisions_state"),
        ("chunk_count = -1", "ck_knowledge_revisions_chunk_count"),
        ("content_fingerprint = 'nothex'", "ck_knowledge_revisions_content_fingerprint"),
    ],
)
def test_revision_checks(admin_engine: Engine, scope: Scope, set_clause: str, name: str) -> None:
    with admin_engine.begin() as conn:
        rev = insert_revision(conn, scope)
    err = fails(
        admin_engine,
        f"UPDATE knowledge_revisions SET {set_clause} WHERE id = :r",
        {"r": rev},
    )
    assert (sqlstate(err), constraint(err)) == ("23514", name)


def test_revision_version_must_belong_to_the_document(
    admin_engine: Engine, scope: Scope, other: Scope
) -> None:
    with pytest.raises(DBAPIError) as exc, admin_engine.begin() as conn:
        insert_revision(conn, scope, version_id=other.version_id)
    assert constraint(exc.value) == "fk_knowledge_revisions_version"


CHECKPOINT_INSERT = (
    "INSERT INTO ingestion_checkpoints (revision_id, stage, org_id, state, input_fingerprint,"
    " artifact_sha256, artifact_bytes, completed_at)"
    " VALUES (:r, :s, :o, :st, 'fp', :sha, :b, CASE WHEN :done THEN now() END)"
)


def checkpoint_params(scope: Scope, rev: uuid.UUID, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "r": rev,
        "s": "extract",
        "o": scope.org_id,
        "st": "running",
        "sha": None,
        "b": None,
        "done": False,
    }
    base.update(over)
    return base


def test_checkpoint_primary_key_is_revision_and_stage(admin_engine: Engine, scope: Scope) -> None:
    with admin_engine.begin() as conn:
        rev = insert_revision(conn, scope)
        conn.execute(text(CHECKPOINT_INSERT), checkpoint_params(scope, rev))
        conn.execute(text(CHECKPOINT_INSERT), checkpoint_params(scope, rev, s="chunk"))
    err = fails(admin_engine, CHECKPOINT_INSERT, checkpoint_params(scope, rev))
    assert (sqlstate(err), constraint(err)) == ("23505", "pk_ingestion_checkpoints")


@pytest.mark.parametrize(
    ("over", "name"),
    [
        ({"s": "ocr"}, "ck_ingestion_checkpoints_stage"),
        ({"st": "paused"}, "ck_ingestion_checkpoints_state"),
        ({"b": -5}, "ck_ingestion_checkpoints_artifact_bytes"),
        ({"sha": "xyz"}, "ck_ingestion_checkpoints_artifact_sha256"),
        ({"st": "completed", "done": False}, "ck_ingestion_checkpoints_completed_at"),
        ({"st": "running", "done": True}, "ck_ingestion_checkpoints_completed_at"),
    ],
)
def test_checkpoint_checks(
    admin_engine: Engine, scope: Scope, over: dict[str, Any], name: str
) -> None:
    with admin_engine.begin() as conn:
        rev = insert_revision(conn, scope)
    err = fails(admin_engine, CHECKPOINT_INSERT, checkpoint_params(scope, rev, **over))
    assert (sqlstate(err), constraint(err)) == ("23514", name)


def test_checkpoint_cannot_claim_another_org(
    admin_engine: Engine, scope: Scope, other: Scope
) -> None:
    with admin_engine.begin() as conn:
        rev = insert_revision(conn, scope)
    err = fails(admin_engine, CHECKPOINT_INSERT, checkpoint_params(other, rev))
    assert (sqlstate(err), constraint(err)) == ("23503", "fk_ingestion_checkpoints_revision")


PUBLICATION_INSERT = (
    "INSERT INTO document_publications (document_id, org_id, workspace_id, revision_id,"
    " version_id, version_number, previous_revision_id)"
    " VALUES (:d, :o, :w, :r, :v, :n, :p)"
)


def publication_params(scope: Scope, rev: uuid.UUID, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "d": scope.document_id,
        "o": scope.org_id,
        "w": scope.workspace_id,
        "r": rev,
        "v": scope.version_id,
        "n": 1,
        "p": None,
    }
    base.update(over)
    return base


def test_one_publication_per_document(admin_engine: Engine, scope: Scope) -> None:
    with admin_engine.begin() as conn:
        first = insert_revision(conn, scope)
        second = insert_revision(conn, scope)
        conn.execute(text(PUBLICATION_INSERT), publication_params(scope, first))
    err = fails(admin_engine, PUBLICATION_INSERT, publication_params(scope, second))
    assert (sqlstate(err), constraint(err)) == ("23505", "pk_document_publications")
    # The pointer swap is an UPDATE of the single row, with the old revision as previous.
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE document_publications SET revision_id = :n, previous_revision_id = :o"
                " WHERE document_id = :d"
            ),
            {"n": second, "o": first, "d": scope.document_id},
        )


def test_publication_cannot_point_at_another_documents_revision(
    admin_engine: Engine, scope: Scope, other: Scope
) -> None:
    with admin_engine.begin() as conn:
        foreign = insert_revision(conn, other)
    err = fails(admin_engine, PUBLICATION_INSERT, publication_params(scope, foreign))
    assert (sqlstate(err), constraint(err)) == ("23503", "fk_document_publications_revision")


def test_publication_previous_revision_must_belong_to_the_document(
    admin_engine: Engine, scope: Scope, other: Scope
) -> None:
    with admin_engine.begin() as conn:
        mine = insert_revision(conn, scope)
        foreign = insert_revision(conn, other)
    err = fails(admin_engine, PUBLICATION_INSERT, publication_params(scope, mine, p=foreign))
    assert constraint(err) == "fk_document_publications_previous_revision"


def test_publication_version_number_is_positive(admin_engine: Engine, scope: Scope) -> None:
    with admin_engine.begin() as conn:
        rev = insert_revision(conn, scope)
    err = fails(admin_engine, PUBLICATION_INSERT, publication_params(scope, rev, n=0))
    assert constraint(err) == "ck_document_publications_version_number"


def test_publication_version_must_belong_to_the_document(
    admin_engine: Engine, scope: Scope, other: Scope
) -> None:
    with admin_engine.begin() as conn:
        rev = insert_revision(conn, scope)
    err = fails(
        admin_engine, PUBLICATION_INSERT, publication_params(scope, rev, v=other.version_id)
    )
    assert constraint(err) == "fk_document_publications_version"


def test_second_version_can_have_its_own_job(admin_engine: Engine, scope: Scope) -> None:
    v2 = add_version(admin_engine, scope, 2)
    with admin_engine.begin() as conn:
        a = insert_job(conn, scope)
        b = insert_job(conn, scope, version_id=v2)
    assert a != b


# -- outbox_events ----------------------------------------------------------------------------


def test_outbox_event_types_and_defaults(admin_engine: Engine, scope: Scope) -> None:
    with admin_engine.begin() as conn:
        for event_type in (
            "ingestion.job.queued",
            "knowledge.revision.published",
            "document.deleted",
            "knowledge.reconcile",
        ):
            insert_outbox(conn, scope, event_type=event_type)
        rows = conn.execute(
            text("SELECT seq, attempts, dispatched_at, id FROM outbox_events WHERE org_id = :o"),
            {"o": scope.org_id},
        ).all()
    assert len(rows) == 4
    assert {r.attempts for r in rows} == {0}
    assert {r.dispatched_at for r in rows} == {None}
    assert len({r.seq for r in rows}) == 4


def test_outbox_rejects_unknown_event_type_and_negative_attempts(
    admin_engine: Engine, scope: Scope
) -> None:
    with pytest.raises(DBAPIError) as exc, admin_engine.begin() as conn:
        insert_outbox(conn, scope, event_type="mystery")
    assert constraint(exc.value) == "ck_outbox_events_type"
    with admin_engine.begin() as conn:
        ev = insert_outbox(conn, scope)
    err = fails(admin_engine, "UPDATE outbox_events SET attempts = -1 WHERE id = :i", {"i": ev})
    assert constraint(err) == "ck_outbox_events_attempts"


def test_outbox_row_needs_a_real_org(admin_engine: Engine, migrated: None) -> None:
    err = fails(
        admin_engine,
        "INSERT INTO outbox_events (org_id, aggregate_type, aggregate_id, event_type)"
        " VALUES (:o, 'x', :a, 'document.deleted')",
        {"o": uuid.uuid4(), "a": uuid.uuid4()},
    )
    assert constraint(err) == "fk_outbox_events_org_id"
