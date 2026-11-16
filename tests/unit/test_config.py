import pytest
from pydantic import ValidationError

from backend.config import Settings
from backend.main import create_app

DB = "postgresql+asyncpg://u:p@localhost:5432/db"
REDIS = "redis://localhost:6379/0"
SECRET = "x" * 32


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"database_url": DB, "redis_url": REDIS, "jwt_secret": SECRET}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_settings_load_from_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EIA_DATABASE_URL", DB)
    monkeypatch.setenv("EIA_REDIS_URL", REDIS)
    monkeypatch.setenv("EIA_JWT_SECRET", SECRET)
    monkeypatch.setenv("EIA_LOG_LEVEL", "DEBUG")
    settings = Settings(_env_file=None)
    assert settings.database_url == DB
    assert settings.log_level == "DEBUG"
    assert settings.environment == "development"


def test_missing_database_url_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EIA_DATABASE_URL", raising=False)
    monkeypatch.setenv("EIA_REDIS_URL", REDIS)
    monkeypatch.setenv("EIA_JWT_SECRET", SECRET)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_missing_jwt_secret_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EIA_JWT_SECRET", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=DB, redis_url=REDIS)


def test_short_jwt_secret_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(jwt_secret="too-short")


def test_jwt_secret_is_hidden_in_repr() -> None:
    assert SECRET not in repr(_settings())


def test_unknown_environment_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(environment="staging")


def test_api_docs_are_disabled_in_production() -> None:
    prod = create_app(_settings(environment="production"))
    dev = create_app(_settings(environment="development"))
    assert prod.docs_url is None and prod.openapi_url is None
    assert dev.docs_url == "/docs"
