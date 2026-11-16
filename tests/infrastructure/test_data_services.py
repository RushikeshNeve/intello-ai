"""M00.02.01 acceptance: PostgreSQL and Redis boot with health checks and persistent volumes.

Run:
    uv run --no-project --with pytest --with "psycopg[binary]" --with redis \
        pytest -q tests/infrastructure/test_data_services.py
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
import redis
from compose_support import ComposeProject, compose_file, require_docker, wait_until

require_docker()

SERVICES = ("postgres", "redis")


@pytest.fixture(scope="module")
def stack() -> Iterator[ComposeProject]:
    project = ComposeProject([compose_file("data.yml")])
    project.down(volumes=True)  # clean slate for this test project only
    try:
        project.up(wait=True)
        yield project
    finally:
        project.down(volumes=True)


def _pg(stack: ComposeProject, *, app: bool = False) -> psycopg.Connection[tuple[object, ...]]:
    env = stack.env
    user = env["POSTGRES_APP_USER"] if app else env["POSTGRES_USER"]
    password = env["POSTGRES_APP_PASSWORD"] if app else env["POSTGRES_PASSWORD"]
    return psycopg.connect(
        host="127.0.0.1",
        port=int(env["POSTGRES_HOST_PORT"]),
        dbname=env["POSTGRES_DB"],
        user=user,
        password=password,
        connect_timeout=5,
        autocommit=True,
    )


def _redis(stack: ComposeProject) -> redis.Redis:
    env = stack.env
    return redis.Redis(
        host="127.0.0.1",
        port=int(env["REDIS_HOST_PORT"]),
        password=env["REDIS_PASSWORD"],
        socket_timeout=5,
        decode_responses=True,
    )


def _wait_for_clients(stack: ComposeProject) -> None:
    stack.wait_healthy(SERVICES)

    def pg_ok() -> bool:
        with _pg(stack) as conn:
            return conn.execute("SELECT 1").fetchone() == (1,)

    wait_until(pg_ok, timeout=60, description="postgres connection")
    wait_until(lambda: _redis(stack).ping(), timeout=60, description="redis ping")


def test_services_healthy_with_volumes_and_loopback_ports(stack: ComposeProject) -> None:
    for service in SERVICES:
        assert stack.health(service) == "healthy", stack.logs(service)
        info = stack.inspect(service)
        mounts = {m["Destination"]: m for m in info["Mounts"]}
        data_dir = "/var/lib/postgresql/data" if service == "postgres" else "/data"
        assert mounts[data_dir]["Type"] == "volume"
        assert mounts[data_dir]["Name"] == f"{stack.project}_{service}-data"
        assert info["HostConfig"]["RestartPolicy"]["Name"] in ("", "no")
        bindings = stack.published_bindings(service)
        assert bindings, f"{service} publishes no ports"
        assert all(ip == "127.0.0.1" for ip, _ in bindings), bindings


def test_secrets_not_exposed_in_container_config(stack: ComposeProject) -> None:
    secrets = [stack.env[k] for k in ("POSTGRES_PASSWORD", "POSTGRES_APP_PASSWORD", "REDIS_PASSWORD")]
    for service in SERVICES:
        config = stack.inspect(service)["Config"]
        exposed = " ".join([*(config.get("Env") or []), *(config.get("Cmd") or [])])
        exposed += " ".join((config.get("Healthcheck") or {}).get("Test") or [])
        for secret in secrets:
            assert secret not in exposed, f"{service} exposes a password in docker inspect"


def test_redis_requires_password_and_uses_aof(stack: ComposeProject) -> None:
    env = stack.env
    anonymous = redis.Redis(host="127.0.0.1", port=int(env["REDIS_HOST_PORT"]), socket_timeout=5)
    with pytest.raises(redis.exceptions.AuthenticationError):
        anonymous.ping()
    assert _redis(stack).config_get("appendonly") == {"appendonly": "yes"}


def test_app_role_is_least_privilege(stack: ComposeProject) -> None:
    app_user = stack.env["POSTGRES_APP_USER"]
    with _pg(stack) as conn:
        row = conn.execute(
            "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication"
            " FROM pg_roles WHERE rolname = %s",
            (app_user,),
        ).fetchone()
        assert row is not None, f"role {app_user} missing"
        assert row == (True, False, False, False, False, False)
        privs = conn.execute(
            "SELECT has_database_privilege(%s, current_database(), 'CONNECT'),"
            " has_schema_privilege(%s, 'public', 'USAGE'),"
            " has_schema_privilege(%s, 'public', 'CREATE')",
            (app_user, app_user, app_user),
        ).fetchone()
        assert privs == (True, True, False)

    with _pg(stack, app=True) as app_conn:
        assert app_conn.execute("SELECT current_user").fetchone() == (app_user,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_conn.execute("CREATE TABLE should_fail (id int)")


def test_data_persists_across_restart_and_recreate(stack: ComposeProject) -> None:
    marker = uuid.uuid4().hex
    with _pg(stack) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS persistence_probe (marker text PRIMARY KEY)")
        conn.execute("INSERT INTO persistence_probe (marker) VALUES (%s)", (marker,))
    _redis(stack).set("persistence_probe", marker)

    def assert_persisted(stage: str) -> None:
        _wait_for_clients(stack)
        with _pg(stack) as conn:
            found = conn.execute(
                "SELECT marker FROM persistence_probe WHERE marker = %s", (marker,)
            ).fetchone()
        assert found == (marker,), f"postgres row lost after {stage}"
        assert _redis(stack).get("persistence_probe") == marker, f"redis key lost after {stage}"

    stack.restart()
    assert_persisted("docker compose restart")

    stack.down(volumes=False)  # containers removed, named volumes kept
    stack.up(wait=True)
    assert_persisted("down (without -v) + up")
