"""Runs /readyz against real PostgreSQL and Redis.

Skipped unless EIA_DATABASE_URL and EIA_REDIS_URL are set, which CI does with
service containers. Locally: ``docker compose up -d postgres redis`` first.
"""

import os

import pytest
from httpx import ASGITransport, AsyncClient

from backend.config import Settings
from backend.main import create_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.getenv("EIA_DATABASE_URL") and os.getenv("EIA_REDIS_URL")),
        reason="EIA_DATABASE_URL and EIA_REDIS_URL are not set",
    ),
]


async def _readyz(settings: Settings) -> tuple[int, dict[str, object]]:
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/readyz")
        return response.status_code, response.json()


async def test_ready_with_real_dependencies() -> None:
    status, body = await _readyz(Settings(_env_file=None))
    assert status == 200
    assert body == {"status": "ready", "checks": {"database": "ok", "redis": "ok"}}


async def test_not_ready_when_database_is_unreachable() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
        readiness_timeout_seconds=1,
    )
    status, body = await _readyz(settings)
    assert status == 503
    assert body["checks"] == {"database": "failed", "redis": "ok"}
