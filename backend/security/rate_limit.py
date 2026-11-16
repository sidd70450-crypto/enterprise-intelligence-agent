"""Limits on failed logins, kept in Redis.

Failures are counted per email and per client IP in a fixed window. Once either
counter reaches its limit, further login attempts are refused, even with the
right password, until the window ends. A successful login clears the email
counter.

If Redis is unavailable the limiter lets attempts through and logs a warning.
Failing closed would lock every user out during a Redis outage; Argon2's cost
still slows guessing in the meantime. See docs/adr/0003-authentication.md.
"""

import hashlib

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = structlog.get_logger("eia.rate_limit")

IP_LIMIT_MULTIPLIER = 5


def _email_key(email: str) -> str:
    # Hashed so raw email addresses are not stored in Redis.
    return "rl:login:email:" + hashlib.sha256(email.encode()).hexdigest()


def _ip_key(ip: str) -> str:
    return "rl:login:ip:" + ip


async def allow_attempt(redis: Redis, key: str, limit: int, window_seconds: int) -> bool:
    """Count one attempt against ``key``; False once ``limit`` is exceeded in the window."""
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.set(key, 0, ex=window_seconds, nx=True)
            pipe.incr(key)
            _, count = await pipe.execute()
    except RedisError as exc:
        log.warning("rate_limit_unavailable", error=type(exc).__name__)
        return True
    return int(count) <= limit


class LoginRateLimiter:
    def __init__(self, redis: Redis, max_failures: int, window_seconds: int) -> None:
        self._redis = redis
        self._limits = (max_failures, max_failures * IP_LIMIT_MULTIPLIER)
        self._window = window_seconds

    def _keys(self, email: str, ip: str | None) -> list[tuple[str, int]]:
        keys = [(_email_key(email), self._limits[0])]
        if ip:
            keys.append((_ip_key(ip), self._limits[1]))
        return keys

    async def is_blocked(self, email: str, ip: str | None) -> bool:
        keys = self._keys(email, ip)
        try:
            counts = await self._redis.mget([key for key, _ in keys])
        except RedisError as exc:
            log.warning("rate_limit_unavailable", error=type(exc).__name__)
            return False
        return any(
            count is not None and int(count) >= limit
            for count, (_, limit) in zip(counts, keys, strict=True)
        )

    async def record_failure(self, email: str, ip: str | None) -> None:
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                for key, _ in self._keys(email, ip):
                    # SET NX starts the window and its expiry together; INCR keeps
                    # the expiry. A counter can therefore never be left without one.
                    pipe.set(key, 0, ex=self._window, nx=True)
                    pipe.incr(key)
                await pipe.execute()
        except RedisError as exc:
            log.warning("rate_limit_unavailable", error=type(exc).__name__)

    async def clear(self, email: str) -> None:
        try:
            await self._redis.delete(_email_key(email))
        except RedisError as exc:
            log.warning("rate_limit_unavailable", error=type(exc).__name__)
