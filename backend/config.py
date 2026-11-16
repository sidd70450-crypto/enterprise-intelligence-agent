"""Typed application settings, read from environment variables.

Every setting is an environment variable prefixed with ``EIA_``. There are no
default credentials or secrets: if the database URL, Redis URL or JWT secret is
missing, the app refuses to start instead of running with something guessable.
"""

from functools import lru_cache
from pathlib import Path
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

    # --- Document ingestion ---
    # Where uploaded files are kept. Shared by the API (writes) and worker (reads).
    storage_dir: Path = Path("data/uploads")
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    max_pdf_pages: int = Field(default=1000, gt=0)
    # Limits for the parser subprocess, which handles untrusted files.
    parse_timeout_seconds: float = Field(default=120.0, gt=0)
    parse_memory_mb: int = Field(default=1024, ge=128)
    # "fastembed" runs a real embedding model locally. "hashing" is a
    # deterministic bag-of-words baseline with no semantic understanding, used
    # in tests and when the model cannot be downloaded.
    embedding_backend: Literal["fastembed", "hashing"] = "fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: Path | None = None
    # Cross-encoder that rescores the top search candidates; "none" skips reranking.
    reranker_backend: Literal["fastembed", "none"] = "fastembed"
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    # --- Language models ---
    # "anthropic" calls the Anthropic API (needs EIA_ANTHROPIC_API_KEY). "fake" is a
    # keyless stand-in that gives mechanical answers, for demos and tests only.
    llm_provider: Literal["anthropic", "fake"] = "anthropic"
    llm_model: str = "claude-sonnet-5-5"
    anthropic_api_key: SecretStr | None = None
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_attempts: int = Field(default=3, ge=1, le=5)
    # Prices in USD per million tokens. Unset means cost is not computed.
    llm_input_price_per_mtok: float | None = Field(default=None, ge=0)
    llm_output_price_per_mtok: float | None = Field(default=None, ge=0)
    task_token_budget: int = Field(default=50_000, gt=0)
    task_max_attempts: int = Field(default=3, gt=0)
    # A running task whose worker has not checked in for this long is resumed elsewhere.
    task_stale_after_seconds: int = Field(default=600, gt=0)

    # Background worker.
    worker_poll_seconds: float = Field(default=1.0, gt=0)
    ingestion_max_attempts: int = Field(default=3, gt=0)
    # A job whose worker has not reported progress for this long is retried.
    ingestion_stale_after_seconds: int = Field(default=600, gt=0)


@lru_cache
def get_settings() -> Settings:
    """Load settings once per process."""
    return Settings()  # values come from the environment
