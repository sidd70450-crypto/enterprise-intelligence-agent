"""/readyz against real PostgreSQL and Redis."""

from httpx import ASGITransport, AsyncClient

from backend.config import Settings
from backend.main import create_app


async def _readyz(settings: Settings) -> tuple[int, dict[str, object]]:
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/readyz")
        return response.status_code, response.json()


async def test_ready_with_real_dependencies(settings: Settings) -> None:
    status, body = await _readyz(settings)
    assert status == 200
    assert body == {"status": "ready", "checks": {"database": "ok", "redis": "ok"}}


async def test_not_ready_when_database_is_unreachable(settings: Settings) -> None:
    unreachable = settings.model_copy(
        update={"database_url": "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none"}
    )
    status, body = await _readyz(unreachable)
    assert status == 503
    assert body["checks"] == {"database": "failed", "redis": "ok"}
