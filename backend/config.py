"""Typed application settings, read from environment variables.

Every setting is an environment variable prefixed with ``EIA_``. There are no
default credentials: if the database or Redis URL is missing, the app refuses to
start instead of silently connecting to something unexpected.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
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


@lru_cache
def get_settings() -> Settings:
    """Load settings once per process."""
    return Settings()  # values come from the environment
