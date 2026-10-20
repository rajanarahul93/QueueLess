import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_missing_required_env_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(ValidationError) as error:
        Settings()
    assert {e["loc"][0] for e in error.value.errors()} == {"database_url", "redis_url"}


def test_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("REDIS_URL", "redis://h:6379/0")
    assert Settings().redis_url == "redis://h:6379/0"
