import secrets
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.config import Settings
from backend.main import create_app

TEST_SECRET = secrets.token_urlsafe(48)


@pytest.fixture
def settings() -> Settings:
    # These URLs are never connected to in unit tests: /readyz checks are
    # replaced with fakes, and the clients only connect on first use.
    return Settings(
        environment="test",
        database_url="postgresql+asyncpg://unused:unused@localhost:1/unused",
        redis_url="redis://localhost:1/0",
        readiness_timeout_seconds=0.05,
        jwt_secret=TEST_SECRET,
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
