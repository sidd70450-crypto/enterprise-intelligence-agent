"""Progress events, published to Redis and streamed to clients.

Events are a live feed for the UI, not the record of what happened: the
database (tasks and workflow_steps) is the record, and a client that connects
late or misses events gets a snapshot from it first.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = structlog.get_logger("eia.events")

Publisher = Callable[[uuid.UUID, dict[str, Any]], Awaitable[None]]


def channel(task_id: uuid.UUID) -> str:
    return f"eia:task:{task_id}"


def redis_publisher(redis: Redis) -> Publisher:
    async def publish(task_id: uuid.UUID, event: dict[str, Any]) -> None:
        try:
            await redis.publish(channel(task_id), json.dumps(event))
        except RedisError as exc:
            # Losing a live update is acceptable; failing the task over it is not.
            log.warning("event_publish_failed", error=type(exc).__name__)

    return publish


async def no_events(task_id: uuid.UUID, event: dict[str, Any]) -> None:
    return None
