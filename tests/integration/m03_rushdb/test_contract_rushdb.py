"""The KnowledgeRepository port contract against BOTH implementations (M03.03.01 acceptance).

``any_repo`` is parametrized over the real adapter (RushDB 3.0.10 + real PostgreSQL for the
organizations) and the in-memory fake; the same behaviours must hold for both.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.modules.knowledge.ports import KnowledgeRepository, OrgScope
from tests.infrastructure_rushdb.contract import ContractTests


class TestKnowledgeRepositoryContract(ContractTests):
    @pytest.fixture
    def repo(self, any_repo: KnowledgeRepository) -> KnowledgeRepository:
        return any_repo

    @pytest.fixture
    def new_scope(self, scope_factory: Callable[[], OrgScope]) -> Callable[[], OrgScope]:
        return scope_factory
