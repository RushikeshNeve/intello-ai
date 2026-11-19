"""The dotted-path factories the worker runtime loads, and the reconcile handler."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pipe_support import Env, SimulatedCrash, document_text, eventually

from app.core.config import Settings
from app.modules.knowledge.publication import DefaultReconcileHandler
from app.workers.pipeline_port import resolve_factory
from app.workers.settings import (
    DEFAULT_DELETE_HANDLER,
    DEFAULT_PIPELINE_RUNNER,
    DEFAULT_RECONCILE_HANDLER,
)


@pytest.mark.parametrize(
    ("path", "attr"),
    [
        (DEFAULT_PIPELINE_RUNNER, "run"),
        (DEFAULT_DELETE_HANDLER, "handle"),
        (DEFAULT_RECONCILE_HANDLER, "handle"),
    ],
)
def test_default_factories_resolve_and_build(path: str, attr: str) -> None:
    factory = resolve_factory(path, setting="test")
    obj = factory(Settings.model_construct())
    assert callable(getattr(obj, attr))


def test_reconcile_handler_flips_and_purges(make_env: Callable[..., Env]) -> None:
    env = make_env()
    scope = env.new_scope()
    v2 = env.job(scope, env.version(scope, document_text("handlerword")))
    v3 = env.job(scope, env.version(scope, document_text("handlerword")))
    env.runner().run(v2, env.ctx(v2))

    def boom(*_a: Any, **_k: Any) -> None:
        raise SimulatedCrash

    env.repo.before["set_published"] = boom
    with pytest.raises(SimulatedCrash):
        env.runner().run(v3, env.ctx(v3))  # pointer moved, flags did not
    env.repo.before.clear()
    handler = DefaultReconcileHandler(sessions=env.sessions, repo=env.repo, artifacts=env.artifacts)
    handler.handle(scope.org_id, scope.document_id)
    handler.handle(scope.org_id, scope.document_id)  # idempotent
    assert eventually(lambda: env.hits_revisions(scope, "handlerword") == {v3.revision_id})
    assert env.rev_state(v2.revision_id) == "purged"
