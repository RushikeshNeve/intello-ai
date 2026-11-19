"""A scriptable fake pipeline and fake handlers for the worker-runtime tests.

Resolved by the real worker process through ``INGEST_PIPELINE_RUNNER=fake_pipeline:build_runner``
(and the two handler variables), so the Celery task, the claim, the heartbeat thread and the
finalizers all run exactly as in production; only the pipeline stages are fake.

Behaviour per job comes from ``<FAKE_RUNNER_DIR>/<job_id>.json``::

    {"1": {"mode": "retryable", "code": "storage_unavailable"}, "default": {"mode": "succeed"}}

keyed by the attempt number of the claim (``default`` otherwise). Every execution appends one line
``<attempt> <epoch> <pid>`` to ``<job_id>.runs``; that file is how tests count executions.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.workers.pipeline_port import (
    ClaimedJob,
    JobContext,
    PermanentJobError,
    PipelineResult,
    RetryableJobError,
)


def _write(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


class FakeRunner:
    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def spec(self, claim: ClaimedJob) -> dict[str, Any]:
        path = self.dir / f"{claim.job_id}.json"
        if not path.exists():
            return {"mode": "succeed"}
        data = json.loads(path.read_text(encoding="utf-8"))
        chosen = data.get(str(claim.attempts), data.get("default", {"mode": "succeed"}))
        return dict(chosen)

    def run(self, claim: ClaimedJob, ctx: JobContext) -> PipelineResult:
        _write(
            self.dir / f"{claim.job_id}.runs", f"{claim.attempts} {claim.lease_epoch} {os.getpid()}"
        )
        spec = self.spec(claim)
        mode = spec["mode"]
        ctx.set_stage("extract")  # a real, fenced write on the job row

        if mode == "succeed":
            return PipelineResult("succeeded")
        if mode == "unsupported":
            return PipelineResult("unsupported", spec.get("code", "unsupported_type"))
        if mode == "no_text":
            return PipelineResult("no_text")
        if mode == "retryable":
            raise RetryableJobError(spec.get("code", "storage_unavailable"), "transient")
        if mode == "permanent":
            raise PermanentJobError(spec.get("code", "pdf_unreadable"), "corrupt")
        if mode == "explode":
            raise ValueError("SECRET-DOCUMENT-CONTENT")  # must never reach the database
        if mode == "long":
            deadline = time.monotonic() + float(spec["seconds"])
            while time.monotonic() < deadline:
                ctx.check_fence()
                time.sleep(0.1)
            return PipelineResult("succeeded")
        if mode in ("block", "block_unaware"):
            _write(self.dir / f"{claim.job_id}.blocked", str(claim.attempts))
            release = self.dir / f"{claim.job_id}.release.{claim.attempts}"
            while not release.exists():
                if mode == "block":
                    ctx.check_fence()
                time.sleep(0.1)
            # An "unaware" (zombie) runner keeps going after its lease was taken away: every
            # write must still be rejected by the database fence.
            ctx.set_stage("embed")
            _write(self.dir / f"{claim.job_id}.zombie_write", str(claim.attempts))
            return PipelineResult("succeeded")
        raise AssertionError(f"unknown mode {mode!r}")


class FakeDeleteHandler:
    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def handle(self, org_id: uuid.UUID, document_id: uuid.UUID) -> None:
        _write(self.dir / "deleted.log", f"{org_id} {document_id}")


class FakeReconcileHandler:
    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def handle(self, org_id: uuid.UUID, document_id: uuid.UUID) -> None:
        _write(self.dir / "reconciled.log", f"{org_id} {document_id}")


def _dir() -> Path:
    return Path(os.environ["FAKE_RUNNER_DIR"])


def build_runner(_settings: Settings) -> FakeRunner:
    return FakeRunner(_dir())


def build_delete_handler(_settings: Settings) -> FakeDeleteHandler:
    return FakeDeleteHandler(_dir())


def build_reconcile_handler(_settings: Settings) -> FakeReconcileHandler:
    return FakeReconcileHandler(_dir())
