"""Helpers for the worker-runtime tests: real worker/dispatcher processes, jobs, scenarios."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from compose_support import ComposeProject, wait_until
from m03_support import SPACE, Scope, rows
from sqlalchemy import Engine, text

from app.core.db import tenant_session
from app.modules.ingestion.jobs import enqueue_ingestion

REPO_ROOT = Path(__file__).resolve().parents[3]
API_DIR = REPO_ROOT / "apps" / "api"
HERE = Path(__file__).resolve().parent


def broker_url(stack: ComposeProject) -> str:
    env = stack.env
    return f"redis://:{env['REDIS_PASSWORD']}@127.0.0.1:{env['REDIS_HOST_PORT']}/1"


def worker_db_url(stack: ComposeProject) -> str:
    from m03_support import make_url

    return make_url(stack, "worker").render_as_string(hide_password=False)


def runtime_env(stack: ComposeProject, fake_dir: Path, **overrides: str) -> dict[str, str]:
    env = {
        **os.environ,
        "UKOS_ENV": "test",
        "DATABASE_WORKER_URL": worker_db_url(stack),
        "CELERY_BROKER_URL": broker_url(stack),
        "FAKE_RUNNER_DIR": str(fake_dir),
        "INGEST_PIPELINE_RUNNER": "fake_pipeline:build_runner",
        "INGEST_DELETE_HANDLER": "fake_pipeline:build_delete_handler",
        "INGEST_RECONCILE_HANDLER": "fake_pipeline:build_reconcile_handler",
        "INGEST_LEASE_SECONDS": "6",
        "INGEST_HEARTBEAT_SECONDS": "2",
        "INGEST_RETRY_BASE_SECONDS": "1",
        "OUTBOX_POLL_SECONDS": "0.2",
        "INGEST_REAP_INTERVAL_SECONDS": "1",
        "INGEST_RECONCILE_INTERVAL_SECONDS": "5",
        "DISPATCHER_HEALTH_FILE": str(fake_dir / "dispatcher.alive"),
        "PYTHONPATH": os.pathsep.join([str(API_DIR), str(HERE)]),
    }
    env.update(overrides)
    return env


class Proc:
    """A subprocess in its own session, so a whole worker tree can be signalled at once."""

    def __init__(self, args: list[str], env: dict[str, str], log: Path) -> None:
        self.log = log
        self._fh = log.open("w", encoding="utf-8")
        self.p = subprocess.Popen(  # noqa: S603
            args,
            cwd=API_DIR,
            env=env,
            stdout=self._fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def text(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")

    def signal_group(self, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.p.pid, sig)

    def stop(self, timeout: float = 30) -> int | None:
        if self.p.poll() is None:
            self.signal_group(signal.SIGTERM)
            try:
                self.p.wait(timeout)
            except subprocess.TimeoutExpired:
                self.signal_group(signal.SIGKILL)
                self.p.wait(10)
        self.signal_group(signal.SIGKILL)  # stray children
        self._fh.close()
        return self.p.returncode

    def kill9(self) -> None:
        self.signal_group(signal.SIGKILL)
        self.p.wait(10)


_counter = 0


def start_worker(env: dict[str, str], fake_dir: Path, *, concurrency: int = 1) -> Proc:
    global _counter
    _counter += 1
    name = f"w{_counter}"
    proc = Proc(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.workers.celery_app",
            "worker",
            "-Q",
            "ingest,maintenance",
            f"--concurrency={concurrency}",
            "-l",
            "info",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "-n",
            f"{name}@%h",
        ],
        env,
        fake_dir / f"{name}.log",
    )
    wait_until(
        lambda: " ready." in proc.text() or proc.p.poll() is not None,
        timeout=60,
        interval=0.3,
        description="celery worker ready",
    )
    assert proc.p.poll() is None, proc.text()
    return proc


def start_dispatcher(env: dict[str, str], fake_dir: Path, tag: str = "d1") -> Proc:
    proc = Proc([sys.executable, "-m", "app.workers.dispatcher"], env, fake_dir / f"{tag}.log")
    health = Path(env["DISPATCHER_HEALTH_FILE"])
    wait_until(
        lambda: health.exists() or proc.p.poll() is not None,
        timeout=30,
        interval=0.2,
        description="dispatcher alive",
    )
    assert proc.p.poll() is None, proc.text()
    return proc


# -- scenarios and observations -----------------------------------------------------------------


def scenario(fake_dir: Path, job_id: uuid.UUID, spec: dict[str, Any]) -> None:
    (fake_dir / f"{job_id}.json").write_text(json.dumps(spec), encoding="utf-8")


def runs(fake_dir: Path, job_id: uuid.UUID) -> list[list[str]]:
    path = fake_dir / f"{job_id}.runs"
    if not path.exists():
        return []
    return [line.split() for line in path.read_text(encoding="utf-8").splitlines() if line]


def new_job(
    app_engine: Engine, admin_engine: Engine, scope: Scope, *, max_attempts: int | None = None
) -> uuid.UUID:
    """A fresh document version plus its job/event/outbox rows, through the real enqueue code."""
    with admin_engine.begin() as conn:
        number = conn.execute(
            text(
                "SELECT coalesce(max(version_number), 0) + 1 FROM document_versions"
                " WHERE document_id = :d"
            ),
            {"d": scope.document_id},
        ).scalar_one()
    from m03_support import add_version

    version_id = add_version(admin_engine, scope, int(number))
    with tenant_session(user_id=None, org_id=scope.org_id, engine=app_engine) as session:
        return enqueue_ingestion(
            session,
            org_id=scope.org_id,
            workspace_id=scope.workspace_id,
            document_id=scope.document_id,
            version_id=version_id,
            embedding_space_id=SPACE,
            max_attempts=max_attempts,
        )


def job(admin_engine: Engine, job_id: uuid.UUID) -> dict[str, Any]:
    with admin_engine.connect() as conn:
        return rows(conn, "SELECT * FROM ingestion_jobs WHERE id = :j", j=job_id)[0]


def events(admin_engine: Engine, job_id: uuid.UUID) -> list[str]:
    with admin_engine.connect() as conn:
        return [
            r["event_type"]
            for r in rows(
                conn,
                "SELECT event_type FROM ingestion_job_events WHERE job_id = :j ORDER BY seq",
                j=job_id,
            )
        ]


def wait_state(
    admin_engine: Engine, job_id: uuid.UUID, states: set[str], timeout: float = 60
) -> dict[str, Any]:
    def probe() -> dict[str, Any] | None:
        row = job(admin_engine, job_id)
        return row if row["state"] in states else None

    return wait_until(probe, timeout=timeout, interval=0.3, description=f"job {job_id} in {states}")


def outbox_for(admin_engine: Engine, job_id: uuid.UUID) -> list[dict[str, Any]]:
    with admin_engine.connect() as conn:
        return rows(
            conn,
            "SELECT * FROM outbox_events WHERE aggregate_id = :j ORDER BY seq",
            j=job_id,
        )


def settle(seconds: float) -> None:
    time.sleep(seconds)
