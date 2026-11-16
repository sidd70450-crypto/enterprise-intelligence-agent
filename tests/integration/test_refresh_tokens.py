"""Refresh-token rotation, reuse detection, expiry and logout."""

import asyncio
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.models import AuditEvent, RefreshToken
from tests.integration.conftest import Login, Register, bearer

REFRESH = "/api/v1/auth/refresh"


def _cookie(token: str) -> dict[str, str]:
    return {"Cookie": f"eia_refresh={token}"}


async def _login_and_capture(client: AsyncClient, login: Login, email: str) -> str:
    await login(email)
    token = client.cookies.get("eia_refresh")
    assert token
    client.cookies.clear()  # send cookies explicitly from here on
    return token


async def test_refresh_rotates_the_token(
    client: AsyncClient, register: Register, login: Login
) -> None:
    await register("ada@example.com")
    first = await _login_and_capture(client, login, "ada@example.com")

    response = await client.post(REFRESH, headers=_cookie(first))
    assert response.status_code == 200
    second = response.cookies.get("eia_refresh")
    assert second and second != first

    me = await client.get("/api/v1/me", headers=bearer(response.json()["access_token"]))
    assert me.status_code == 200


async def test_reusing_a_rotated_token_revokes_the_whole_session(
    client: AsyncClient,
    register: Register,
    login: Login,
    db: async_sessionmaker[AsyncSession],
) -> None:
    await register("ada@example.com")
    first = await _login_and_capture(client, login, "ada@example.com")
    second = (await client.post(REFRESH, headers=_cookie(first))).cookies["eia_refresh"]

    # An attacker replays the stolen first token.
    replay = await client.post(REFRESH, headers=_cookie(first))
    assert replay.status_code == 401

    # The legitimate holder of the second token is logged out too.
    assert (await client.post(REFRESH, headers=_cookie(second))).status_code == 401

    async with db() as session:
        live = await session.scalar(
            select(func.count()).select_from(RefreshToken).where(RefreshToken.revoked_at.is_(None))
        )
        detected = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "auth.refresh_reuse_detected")
        )
    assert live == 0
    assert detected == 1


async def test_concurrent_refreshes_cannot_both_succeed(
    client: AsyncClient, register: Register, login: Login
) -> None:
    await register("ada@example.com")
    token = await _login_and_capture(client, login, "ada@example.com")

    results = await asyncio.gather(
        *(client.post(REFRESH, headers=_cookie(token)) for _ in range(2))
    )
    # Row locking serialises the two requests: one rotates the token and the
    # other arrives to find it already used. Two successful rotations would fork
    # the session into two valid token chains.
    assert sorted(r.status_code for r in results) == [200, 401]


async def test_expired_refresh_token_is_rejected(
    client: AsyncClient,
    register: Register,
    login: Login,
    db: async_sessionmaker[AsyncSession],
) -> None:
    await register("ada@example.com")
    token = await _login_and_capture(client, login, "ada@example.com")
    async with db() as session:
        await session.execute(
            update(RefreshToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

    response = await client.post(REFRESH, headers=_cookie(token))
    assert response.status_code == 401
    assert "max-age=0" in response.headers["set-cookie"].lower()  # cookie cleared


async def test_logout_revokes_the_session(
    client: AsyncClient, register: Register, login: Login
) -> None:
    await register("ada@example.com")
    token = await _login_and_capture(client, login, "ada@example.com")

    logout = await client.post("/api/v1/auth/logout", headers=_cookie(token))
    assert logout.status_code == 204
    assert (await client.post(REFRESH, headers=_cookie(token))).status_code == 401


async def test_unknown_or_missing_refresh_token_is_rejected(client: AsyncClient) -> None:
    assert (await client.post(REFRESH)).status_code == 401
    assert (await client.post(REFRESH, headers=_cookie("made-up-token"))).status_code == 401
