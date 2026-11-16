"""compose_support: OS environment overlays env-file values (parallel isolated stacks)."""

from __future__ import annotations

import pytest
from compose_support import ComposeProject, compose_file, overlay_os_environ


def test_overlay_prefers_os_environ_for_known_keys_only() -> None:
    values = {"POSTGRES_HOST_PORT": "5532", "REDIS_HOST_PORT": "6479"}
    environ = {"POSTGRES_HOST_PORT": "25532", "UNRELATED": "x"}
    assert overlay_os_environ(values, environ) == {
        "POSTGRES_HOST_PORT": "25532",
        "REDIS_HOST_PORT": "6479",
    }


def test_compose_project_env_reflects_os_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_HOST_PORT", "26999")
    project = ComposeProject([compose_file("data.yml")], project="ukos-m01-foundation-unused")
    assert project.env["REDIS_HOST_PORT"] == "26999"
    project = ComposeProject(
        [compose_file("data.yml")],
        project="ukos-m01-foundation-unused",
        extra_env={"REDIS_HOST_PORT": "27000"},
    )
    assert project.env["REDIS_HOST_PORT"] == "27000"  # explicit extra_env still wins
