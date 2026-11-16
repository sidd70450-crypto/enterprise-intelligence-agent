"""Application factory.

``create_app`` builds the FastAPI app from a ``Settings`` object. Tests pass
their own settings; production uses ``app`` at the bottom, which reads the
environment.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from backend.api.health import router as health_router
from backend.api.middleware import RequestIdMiddleware
from backend.config import Settings, get_settings
from backend.logging_setup import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Neither client connects here; connections open on first use. /readyz
        # is what reports whether the dependencies are actually reachable.
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        redis = Redis.from_url(settings.redis_url)
        app.state.settings = settings
        app.state.engine = engine
        app.state.redis = redis
        try:
            yield
        finally:
            await redis.aclose()
            await engine.dispose()

    is_production = settings.environment == "production"
    app = FastAPI(
        title="Enterprise Intelligence Agent",
        version="0.1.0.dev0",
        lifespan=lifespan,
        # Interactive docs are a development aid; they are off in production.
        docs_url=None if is_production else "/docs",
        redoc_url=None,
        openapi_url=None if is_production else "/openapi.json",
    )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router)
    return app


def app() -> FastAPI:
    """Entry point for ``uvicorn backend.main:app --factory``."""
    return create_app()
