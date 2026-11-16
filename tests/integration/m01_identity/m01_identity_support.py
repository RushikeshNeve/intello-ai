"""Real-Dex helpers for the M01 identity integration tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
from compose_support import ComposeProject, compose_file, wait_until

DEX_PROJECT = os.environ.get("UKOS_TEST_DEX_PROJECT", "ukos-m01-identity")
DEX_PORT = int(os.environ.get("UKOS_TEST_DEX_PORT", "28381"))
DEX_EXP_PROJECT = os.environ.get("UKOS_TEST_DEX_EXP_PROJECT", "ukos-m01-identity-exp")
DEX_EXP_PORT = int(os.environ.get("UKOS_TEST_DEX_EXP_PORT", "28382"))
DEX_EXP_TTL_SECONDS = 2


@dataclass(frozen=True)
class Dex:
    project: ComposeProject
    issuer: str
    client_id: str
    client_secret: str

    def token(
        self,
        username: str = "alice@ukos.test",
        password: str = "ukos-dev-alice-password",  # noqa: S107 - test user (identity/README.md)
        *,
        audience: str | None = "ukos-api",
        kind: str = "id_token",
    ) -> str:
        scope = "openid email profile"
        if audience:
            scope += f" audience:server:client_id:{audience}"
        res = httpx.post(
            f"{self.issuer}/token",
            auth=(self.client_id, self.client_secret),
            data={
                "grant_type": "password",
                "username": username,
                "password": password,
                "scope": scope,
            },
            timeout=10,
        )
        res.raise_for_status()
        return str(res.json()[kind])


def start_dex(project_name: str, port: int, **extra: str) -> Iterator[Dex]:
    issuer = f"http://127.0.0.1:{port}/dex"
    project = ComposeProject(
        [compose_file("identity.yml")],
        project=project_name,
        extra_env={"OIDC_HOST_PORT": str(port), "OIDC_ISSUER_URL": issuer, **extra},
    )
    project.down(volumes=True)
    try:
        project.up(wait=True)
        wait_until(
            lambda: httpx.get(f"{issuer}/keys", timeout=5).status_code == 200,
            timeout=60,
            description=f"{project_name} JWKS",
        )
        yield Dex(
            project=project,
            issuer=issuer,
            client_id=project.env.get("OIDC_CLIENT_ID", "ukos-web"),
            client_secret=project.env["OIDC_CLIENT_SECRET"],
        )
    finally:
        if os.environ.get("UKOS_TEST_KEEP_STACK") != "1":
            project.down(volumes=True)
