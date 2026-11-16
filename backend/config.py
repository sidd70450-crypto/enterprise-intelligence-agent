"""Typed application settings, read from environment variables.

Every setting is an environment variable prefixed with ``EIA_``. There are no
default credentials or secrets: if the database URL, Redis URL or JWT secret is
missing, the app refuses to start instead of running with something guessable.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EIA_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # e.g. postgresql+asyncpg://user:password@host:5432/dbname
    database_url: str = Field(min_length=1)
    # e.g. redis://host:6379/0
    redis_url: str = Field(min_length=1)

    # Upper bound, in seconds, for each dependency check made by /readyz.
    readiness_timeout_seconds: float = Field(default=2.0, gt=0)

    # HMAC key for access tokens. Generate one with:
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    jwt_secret: SecretStr = Field(min_length=32)
    access_token_ttl_seconds: int = Field(default=900, gt=0, le=3600)
    refresh_token_ttl_seconds: int = Field(default=14 * 24 * 3600, gt=0)

    # Failed logins allowed per email inside the window; per IP it is five times this.
    login_max_failures: int = Field(default=10, gt=0)
    login_window_seconds: int = Field(default=900, gt=0)

    # Account registrations allowed per client IP per hour.
    registrations_per_ip_per_hour: int = Field(default=10, gt=0)


@lru_cache
def get_settings() -> Settings:
    """Load settings once per process."""
    return Settings()  # values come from the environment
