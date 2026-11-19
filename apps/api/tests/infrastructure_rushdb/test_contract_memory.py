"""The port contract against the in-memory fake (real RushDB: tests/integration/m03_rushdb)."""

from __future__ import annotations

import pytest

from app.infrastructure.rushdb.memory import InMemoryKnowledgeRepository
from app.modules.knowledge.ports import KnowledgeRepository
from tests.infrastructure_rushdb.contract import ContractTests
from tests.infrastructure_rushdb.support import ScopeFactory, fake_scope


class TestInMemoryContract(ContractTests):
    @pytest.fixture
    def repo(self) -> KnowledgeRepository:
        return InMemoryKnowledgeRepository()

    @pytest.fixture
    def new_scope(self) -> ScopeFactory:
        return fake_scope
