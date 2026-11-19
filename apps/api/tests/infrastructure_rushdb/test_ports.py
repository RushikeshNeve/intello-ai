"""DTO validation and scope construction rules (no network)."""

from __future__ import annotations

import dataclasses
import math
import uuid
from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.modules.knowledge.ports import (
    ChunkRecord,
    OrgScope,
    SearchFilters,
    UnknownOrganization,
    validate_limit,
    validate_query_text,
    validate_space_id,
    validate_vector,
    validated_org_scope,
)
from tests.infrastructure_rushdb.support import FakeSession, fake_scope, make_chunk


def test_org_scope_cannot_be_constructed_directly() -> None:
    org = uuid.uuid4()
    with pytest.raises(TypeError, match="validated_org_scope"):
        OrgScope(org)
    with pytest.raises(TypeError, match="validated_org_scope"):
        OrgScope(org, _mint=object())
    with pytest.raises(TypeError):
        OrgScope(str(org))  # type: ignore[arg-type]


def test_scope_comes_only_from_a_live_org_and_derives_the_tenant() -> None:
    org = uuid.uuid4()
    session = FakeSession([org])
    scope = validated_org_scope(cast(Session, session), org)
    assert scope.org_id == org
    assert scope.tenant == f"org:{org}"
    assert len(session.queries) == 1
    assert "deleted_at IS NULL" in session.queries[0]
    assert "organizations" in session.queries[0]
    assert repr(scope) == f"OrgScope(org_id=UUID('{org}'))"


def test_unknown_or_deleted_org_is_rejected() -> None:
    with pytest.raises(UnknownOrganization):
        validated_org_scope(cast(Session, FakeSession([])), uuid.uuid4())
    with pytest.raises(UnknownOrganization):
        validated_org_scope(cast(Session, FakeSession([])), "not-a-uuid")  # type: ignore[arg-type]


def test_scope_is_immutable() -> None:
    scope = fake_scope()
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.org_id = uuid.uuid4()  # type: ignore[misc]


def test_search_filters_require_a_non_empty_frozenset_of_uuids() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        SearchFilters(frozenset())
    with pytest.raises(TypeError):
        SearchFilters({uuid.uuid4()})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SearchFilters(frozenset({"w1"}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at most"):
        SearchFilters(frozenset(uuid.uuid4() for _ in range(1001)))
    assert SearchFilters(frozenset({uuid.uuid4()}))


def test_chunk_record_validation() -> None:
    scope, ws = fake_scope(), uuid.uuid4()
    good = make_chunk(scope, ws, text="hello world")
    assert good.chunk_index == 0

    def bad(**changes: object) -> ChunkRecord:
        return dataclasses.replace(good, **changes)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="chunk_id"):
        bad(chunk_id=uuid.uuid4())
    with pytest.raises(ValueError, match="chunk_index"):
        bad(chunk_index=-1)
    with pytest.raises(ValueError, match="text must not be empty"):
        bad(text="   ")
    with pytest.raises(ValueError, match="at most"):
        bad(text="x" * 200_001)
    with pytest.raises(ValueError, match="token_estimate"):
        bad(token_estimate=-1)
    with pytest.raises(ValueError, match="content_hash"):
        bad(content_hash="")
    with pytest.raises(ValueError, match="embedding_space_id"):
        bad(embedding_space_id="bad space!")
    with pytest.raises(ValueError, match="1-based"):
        bad(page_start=0, page_end=1)
    with pytest.raises(ValueError, match="page_end"):
        bad(page_start=3, page_end=2)
    with pytest.raises(ValueError, match="both set"):
        bad(page_start=3, page_end=None)
    with pytest.raises(ValueError, match="JSON"):
        bad(locators=[{"x": object()}])
    with pytest.raises(ValueError, match="JSON"):
        bad(locators=[{"x": math.nan}])
    with pytest.raises(ValueError, match="too large"):
        bad(heading_path=["x" * 200_000])
    with pytest.raises(ValueError, match="strings"):
        bad(heading_path=[1])


def test_validators() -> None:
    assert validate_space_id("e0123456789ab") == "e0123456789ab"
    for bad_space in ("", "-x", "a b", "a/b", "x" * 65, "é"):
        with pytest.raises(ValueError, match="embedding_space_id"):
            validate_space_id(bad_space)
    validate_vector([0.0, 1.0], 2)
    for vec in ([], [1.0, True], ["a", 1.0]):
        with pytest.raises(ValueError, match="vector"):
            validate_vector(vec)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="dimensions"):
        validate_vector([1.0], 2)
    for bad_limit in (0, 201, True):
        with pytest.raises(ValueError, match="limit"):
            validate_limit(bad_limit)
    assert validate_limit(200) == 200
    assert validate_query_text("  hi  ") == "hi"
    with pytest.raises(ValueError, match="at most"):
        validate_query_text("x" * 2001)
