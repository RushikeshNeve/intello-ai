from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.core.config import Settings, to_libpq_dsn, to_sqlalchemy_url
from app.core.db import dispose_engines, get_engine
from app.core.models import RLS_TABLES, Base

SECRET = "pw-do-not-leak"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@h:1/d", "postgresql+psycopg://u:p@h:1/d"),
        ("postgresql://u:p@h:1/d", "postgresql+psycopg://u:p@h:1/d"),
        ("postgresql+psycopg2://u:p@h:1/d", "postgresql+psycopg://u:p@h:1/d"),
        ("postgresql+psycopg://u:p@h:1/d", "postgresql+psycopg://u:p@h:1/d"),
    ],
)
def test_to_sqlalchemy_url(raw: str, expected: str) -> None:
    assert to_sqlalchemy_url(raw) == expected


def test_to_libpq_dsn() -> None:
    assert to_libpq_dsn("postgresql+psycopg://u@h/d") == "postgresql://u@h/d"


def test_engine_is_cached_and_never_renders_password() -> None:
    settings = Settings(DATABASE_URL=SecretStr(f"postgresql://ukos_app:{SECRET}@127.0.0.1:1/ukos"))
    try:
        engine = get_engine(settings)
        assert get_engine(settings) is engine
        assert engine.dialect.driver == "psycopg"
        assert SECRET not in repr(engine)
        assert SECRET not in str(engine.url)
        assert engine.pool.size() == 5  # type: ignore[attr-defined]
    finally:
        dispose_engines()


def test_models_cover_contract_tables() -> None:
    assert set(Base.metadata.tables) == set(RLS_TABLES)
