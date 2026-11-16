"""Liveness and readiness endpoints.

``/healthz`` answers "is the process running?" and never touches a dependency.
``/readyz`` answers "can this process serve requests?" by checking PostgreSQL
and Redis, each under a timeout, and returns 503 if any check fails.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

Check = Callable[[], Awaitable[None]]
CheckStatus = Literal["ok", "failed"]

router = APIRouter(tags=["health"])
log = structlog.get_logger("eia.health")


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckStatus]


async def check_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def check_redis(client: Redis) -> None:
    await client.ping()


def get_readiness_checks(request: Request) -> dict[str, Check]:
    """The dependency checks /readyz runs. Tests override this with fakes."""
    engine: AsyncEngine = request.app.state.engine
    redis: Redis = request.app.state.redis
    return {
        "database": lambda: check_database(engine),
        "redis": lambda: check_redis(redis),
    }


def get_readiness_timeout(request: Request) -> float:
    timeout: float = request.app.state.settings.readiness_timeout_seconds
    return timeout


async def _run_check(name: str, check: Check, limit_seconds: float) -> CheckStatus:
    try:
        async with asyncio.timeout(limit_seconds):
            await check()
    except Exception as exc:
        # The reason goes to the logs only: error text can contain hostnames or
        # credentials and must not be returned to an unauthenticated caller.
        log.warning("readiness_check_failed", check=name, error=type(exc).__name__)
        return "failed"
    return "ok"


@router.get("/healthz")
async def healthz() -> HealthResponse:
    return HealthResponse()


@router.get("/readyz", responses={503: {"model": ReadinessResponse}})
async def readyz(
    response: Response,
    checks: Annotated[dict[str, Check], Depends(get_readiness_checks)],
    limit_seconds: Annotated[float, Depends(get_readiness_timeout)],
) -> ReadinessResponse:
    names = list(checks)
    results = await asyncio.gather(*(_run_check(n, checks[n], limit_seconds) for n in names))
    outcome = dict(zip(names, results, strict=True))
    ready = all(result == "ok" for result in results)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ready" if ready else "not_ready", checks=outcome)
