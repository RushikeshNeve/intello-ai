"""Shared helpers for Docker Compose infrastructure tests (owned by the platform agent, M00.02).

Usage::

    from compose_support import ComposeProject, compose_file, require_docker

    stack = ComposeProject([compose_file("data.yml")])   # project "ukos-platform"
    stack.down(volumes=True)                             # clean slate
    stack.up(wait=True)
    stack.env["POSTGRES_HOST_PORT"]                      # values from the env file
    stack.down(volumes=True)

Every command is scoped with ``-p <project>`` so tests never touch containers or volumes that
belong to other Compose projects running on the same Docker engine. Override the project with
``UKOS_TEST_COMPOSE_PROJECT`` (e.g. for parallel CI runs).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DIR = REPO_ROOT / "infrastructure" / "compose"
ENV_EXAMPLE = REPO_ROOT / "infrastructure" / "env" / ".env.example"
DEFAULT_PROJECT = os.environ.get("UKOS_TEST_COMPOSE_PROJECT", "ukos-platform")

T = TypeVar("T")


def compose_file(name: str) -> Path:
    """Path of ``infrastructure/compose/<name>``."""
    return COMPOSE_DIR / name


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv file (KEY=VALUE lines, ``#`` comments, optional surrounding quotes)."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def docker_available() -> bool:
    """True when the docker CLI exists and the engine answers."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def require_docker() -> None:
    """Skip the calling test/module when Docker is not usable."""
    import pytest

    if not docker_available():
        pytest.skip("Docker engine not available", allow_module_level=True)


def wait_until(
    probe: Callable[[], T | None],
    *,
    timeout: float = 60.0,
    interval: float = 1.0,
    description: str = "condition",
) -> T:
    """Call ``probe`` until it returns a truthy value (returned) or ``timeout`` elapses.

    Exceptions raised by ``probe`` count as "not yet"; the last one is chained on timeout.
    """
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while True:
        try:
            result = probe()
            if result:
                return result
        except Exception as exc:  # noqa: BLE001 - probing, retried until timeout
            last_error = exc
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out after {timeout}s waiting for {description}") from last_error
        time.sleep(interval)


class ComposeError(RuntimeError):
    """A docker compose command failed."""


class ComposeProject:
    """Thin wrapper around ``docker compose -p <project> --env-file <env> -f <files...>``."""

    def __init__(
        self,
        files: Sequence[Path],
        *,
        project: str = DEFAULT_PROJECT,
        env_file: Path = ENV_EXAMPLE,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self.files = [Path(f) for f in files]
        self.project = project
        self.env_file = Path(env_file)
        self.extra_env = dict(extra_env or {})
        self.env: dict[str, str] = {**load_env_file(self.env_file), **self.extra_env}

    # -- low level -----------------------------------------------------------------
    def _base(self) -> list[str]:
        cmd = ["docker", "compose", "-p", self.project, "--env-file", str(self.env_file)]
        for f in self.files:
            cmd += ["-f", str(f)]
        return cmd

    def run(
        self, *args: str, check: bool = True, timeout: float = 900.0
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, **self.extra_env}
        # Never let an ambient COMPOSE_* variable redirect commands to another project.
        env.pop("COMPOSE_PROJECT_NAME", None)
        env.pop("COMPOSE_FILE", None)
        result = subprocess.run(
            [*self._base(), *args],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if check and result.returncode != 0:
            raise ComposeError(
                f"`docker compose {' '.join(args)}` exited {result.returncode}\n"
                f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
            )
        return result

    # -- lifecycle -----------------------------------------------------------------
    def up(
        self,
        *services: str,
        wait: bool = True,
        build: bool = False,
        wait_timeout: int = 300,
        timeout: float = 1800.0,
    ) -> None:
        args = ["up", "-d"]
        if build:
            args.append("--build")
        if wait:
            args += ["--wait", "--wait-timeout", str(wait_timeout)]
        self.run(*args, *services, timeout=timeout)

    def down(self, *, volumes: bool = False) -> None:
        args = ["down", "--remove-orphans"]
        if volumes:
            args.append("-v")
        self.run(*args)

    def restart(self, *services: str) -> None:
        self.run("restart", *services)

    def stop(self, *services: str) -> None:
        self.run("stop", *services)

    def start(self, *services: str) -> None:
        self.run("start", *services)

    def logs(self, *services: str, tail: int = 200) -> str:
        return self.run("logs", "--no-color", "--tail", str(tail), *services, check=False).stdout

    # -- inspection ----------------------------------------------------------------
    def container_id(self, service: str) -> str:
        cid = self.run("ps", "-a", "-q", service).stdout.strip().splitlines()
        if not cid:
            raise ComposeError(f"no container for service {service!r} in project {self.project!r}")
        return cid[0]

    def inspect(self, service: str) -> dict[str, Any]:
        out = subprocess.run(
            ["docker", "inspect", self.container_id(service)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        data: list[dict[str, Any]] = json.loads(out)
        return data[0]

    def health(self, service: str) -> str:
        """Container health status: healthy | unhealthy | starting | none | <state>."""
        state = self.inspect(service)["State"]
        health = state.get("Health")
        if health is None:
            return "none" if state.get("Running") else str(state.get("Status"))
        return str(health.get("Status"))

    def wait_healthy(self, services: Iterable[str], *, timeout: float = 180.0) -> None:
        for service in services:
            wait_until(
                lambda s=service: self.health(s) == "healthy",
                timeout=timeout,
                interval=1.0,
                description=f"{self.project}/{service} to become healthy",
            )

    def published_bindings(self, service: str) -> list[tuple[str, str]]:
        """(host_ip, host_port) pairs published by the service's container."""
        ports = self.inspect(service)["NetworkSettings"]["Ports"] or {}
        pairs: list[tuple[str, str]] = []
        for bindings in ports.values():
            for b in bindings or []:
                pairs.append((b["HostIp"], b["HostPort"]))
        return pairs
