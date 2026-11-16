import pytest
from pydantic import ValidationError

from backend.config import Settings
from backend.main import create_app

DB = "postgresql+asyncpg://u:p@localhost:5432/db"
REDIS = "redis://localhost:6379/0"


def test_settings_load_from_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EIA_DATABASE_URL", DB)
    monkeypatch.setenv("EIA_REDIS_URL", REDIS)
    monkeypatch.setenv("EIA_LOG_LEVEL", "DEBUG")
    settings = Settings(_env_file=None)
    assert settings.database_url == DB
    assert settings.log_level == "DEBUG"
    assert settings.environment == "development"


def test_missing_database_url_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EIA_DATABASE_URL", raising=False)
    monkeypatch.setenv("EIA_REDIS_URL", REDIS)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_unknown_environment_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="staging", database_url=DB, redis_url=REDIS)


def test_api_docs_are_disabled_in_production() -> None:
    prod = create_app(Settings(environment="production", database_url=DB, redis_url=REDIS))
    dev = create_app(Settings(environment="development", database_url=DB, redis_url=REDIS))
    assert prod.docs_url is None and prod.openapi_url is None
    assert dev.docs_url == "/docs"
