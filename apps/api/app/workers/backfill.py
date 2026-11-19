"""One-time backfill (M03 contract §11): ``python -m app.workers.backfill [--dry-run]``.

Enqueues ingestion for document versions that have no job for the current pipeline and embedding
space (documents uploaded during M02, or after the embedding space changed). By default only each
live document's **latest** version is considered (an older version can never overwrite a newer
publication); ``--all-versions`` includes every unpurged version.

Cross-org discovery is the one thing the worker role cannot do (it has no org-listing function, by
design: contract §4 lists exactly six definer functions). The operator tool therefore *reads* the
candidates through the admin connection (``DATABASE_MIGRATION_URL``, in a READ ONLY transaction) and
*writes* through the least-privilege worker role under each org's context, using the same
:func:`~app.modules.ingestion.jobs.enqueue_ingestion` the API uses. That keeps the admin credential
out of every long-running service. Idempotent: ``enqueue_ingestion`` is ``ON CONFLICT DO NOTHING``
on the deterministic revision id, so re-running (or racing finalize) never duplicates a job.
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import Engine, text

from app.core.db import create_db_engine
from app.modules.ingestion.ids import PIPELINE_VERSION
from app.modules.ingestion.jobs import enqueue_ingestion
from app.workers.settings import WorkerSettings, get_worker_settings
from app.workers.worker_db import get_worker_engine, worker_session

log = logging.getLogger("ukos.backfill")

_DISCOVER = text(
    "SELECT d.org_id, d.workspace_id, d.id AS document_id, v.id AS version_id"
    " FROM documents d JOIN document_versions v ON v.document_id = d.id"
    " WHERE d.deleted_at IS NULL AND v.purged_at IS NULL"
    "   AND (:all_versions OR v.version_number = ("
    "        SELECT max(v2.version_number) FROM document_versions v2"
    "        WHERE v2.document_id = d.id AND v2.purged_at IS NULL))"
    "   AND NOT EXISTS (SELECT 1 FROM ingestion_jobs j WHERE j.version_id = v.id"
    "        AND j.pipeline_version = :pv AND j.embedding_space_id = :sp)"
    " ORDER BY d.created_at, d.id, v.version_number"
    " LIMIT :n"
)


@dataclass(frozen=True)
class Target:
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID


def discover(
    admin_engine: Engine,
    *,
    space_id: str,
    pipeline_version: str = PIPELINE_VERSION,
    all_versions: bool = False,
    limit: int = 10_000,
) -> list[Target]:
    with admin_engine.connect() as conn:
        trans = conn.begin()
        conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = conn.execute(
            _DISCOVER,
            {"all_versions": all_versions, "pv": pipeline_version, "sp": space_id, "n": limit},
        ).all()
        trans.rollback()
    return [Target(r[0], r[1], r[2], r[3]) for r in rows]


def enqueue_targets(
    worker_engine: Engine,
    targets: list[Target],
    *,
    space_id: str,
    max_attempts: int | None = None,
    batch_size: int = 100,
) -> int:
    """Enqueue every target, one transaction per org batch under that org's context."""
    by_org: dict[uuid.UUID, list[Target]] = defaultdict(list)
    for target in targets:
        by_org[target.org_id].append(target)
    count = 0
    for org_id, items in by_org.items():
        for start in range(0, len(items), batch_size):
            with worker_session(worker_engine, org_id) as session:
                for target in items[start : start + batch_size]:
                    enqueue_ingestion(
                        session,
                        org_id=target.org_id,
                        workspace_id=target.workspace_id,
                        document_id=target.document_id,
                        version_id=target.version_id,
                        embedding_space_id=space_id,
                        max_attempts=max_attempts,
                    )
                    count += 1
    return count


def run(
    settings: WorkerSettings,
    *,
    dry_run: bool,
    all_versions: bool,
    limit: int,
    admin_engine: Engine | None = None,
    worker_engine: Engine | None = None,
) -> tuple[int, int]:
    """Returns ``(candidates, enqueued)``."""
    admin = admin_engine or create_db_engine(
        settings.sqlalchemy_migration_url, pool_size=1, max_overflow=0
    )
    targets = discover(
        admin, space_id=settings.embedding_space_id, all_versions=all_versions, limit=limit
    )
    if dry_run or not targets:
        return len(targets), 0
    enqueued = enqueue_targets(
        worker_engine or get_worker_engine(settings),
        targets,
        space_id=settings.embedding_space_id,
        max_attempts=settings.INGEST_MAX_ATTEMPTS,
    )
    return len(targets), enqueued


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.workers.backfill")
    parser.add_argument("--dry-run", action="store_true", help="count candidates, enqueue nothing")
    parser.add_argument("--all-versions", action="store_true", help="every unpurged version")
    parser.add_argument("--limit", type=int, default=10_000, help="at most this many candidates")
    args = parser.parse_args(argv)
    settings = get_worker_settings()
    logging.basicConfig(level=settings.UKOS_LOG_LEVEL.upper(), format="%(levelname)s %(message)s")
    candidates, enqueued = run(
        settings, dry_run=args.dry_run, all_versions=args.all_versions, limit=args.limit
    )
    print(f"candidates={candidates} enqueued={enqueued} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
