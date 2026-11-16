"""Registration, login, access tokens and permission checks against a real database."""

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.models import AuditEvent, User
from backend.security.tokens import create_access_token
from tests.integration.conftest import (
    PASSWORD,
    TEST_SECRET,
    AddMember,
    Login,
    Register,
    bearer,
)


async def test_register_login_and_me(client: AsyncClient, register: Register, login: Login) -> None:
    account = await register("Ada@Example.com", org="Analytical Engines")
    assert account["email"] == "ada@example.com"
    assert account["role"] == "owner"

    token = await login("ADA@example.com")
    response = await client.get("/api/v1/me", headers=bearer(token))
    assert response.status_code == 200
    me = response.json()
    assert me["email"] == "ada@example.com"
    assert me["organization_id"] == account["organization_id"]
    assert me["organization_name"] == "Analytical Engines"
    assert me["role"] == "owner"
    assert "members:read" in me["permissions"]


async def test_login_sets_a_locked_down_refresh_cookie(
    client: AsyncClient, register: Register
) -> None:
    await register("ada@example.com")
    response = await client.post(
        "/api/v1/auth/login", json={"email": "ada@example.com", "password": PASSWORD}
    )
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("eia_refresh=")
    for attribute in ("httponly", "secure", "samesite=strict", "path=/api/v1/auth"):
        assert attribute in cookie
    assert response.headers["cache-control"] == "no-store"
    assert "refresh" not in response.json()


async def test_duplicate_email_is_rejected_regardless_of_case(
    client: AsyncClient, register: Register
) -> None:
    await register("ada@example.com")
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "ADA@example.com", "password": PASSWORD, "organization_name": "Other"},
    )
    assert response.status_code == 409


async def test_wrong_password_and_unknown_email_look_identical(
    client: AsyncClient, register: Register
) -> None:
    await register("ada@example.com")
    wrong = await client.post(
        "/api/v1/auth/login", json={"email": "ada@example.com", "password": "wrong password!"}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]
    assert "set-cookie" not in wrong.headers


async def test_expired_access_token_is_rejected(client: AsyncClient, register: Register) -> None:
    account = await register("ada@example.com")
    expired = create_access_token(
        uuid.UUID(account["user_id"]),
        uuid.UUID(account["organization_id"]),
        TEST_SECRET,
        ttl_seconds=60,
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    response = await client.get("/api/v1/me", headers=bearer(expired))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_token_for_an_organization_the_user_is_not_in_is_rejected(
    client: AsyncClient, register: Register
) -> None:
    ada = await register("ada@example.com", org="Org A")
    bob = await register("bob@example.com", org="Org B")
    # Correctly signed, but claims Ada belongs to Bob's organization.
    forged = create_access_token(
        uuid.UUID(ada["user_id"]), uuid.UUID(bob["organization_id"]), TEST_SECRET, 60
    )
    response = await client.get("/api/v1/me", headers=bearer(forged))
    assert response.status_code == 401


async def test_deactivated_user_loses_access_immediately(
    client: AsyncClient,
    register: Register,
    login: Login,
    db: async_sessionmaker[AsyncSession],
) -> None:
    await register("ada@example.com")
    token = await login("ada@example.com")
    async with db() as session:
        await session.execute(
            update(User).where(User.email == "ada@example.com").values(is_active=False)
        )
        await session.commit()

    assert (await client.get("/api/v1/me", headers=bearer(token))).status_code == 401
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401
    relogin = await client.post(
        "/api/v1/auth/login", json={"email": "ada@example.com", "password": PASSWORD}
    )
    assert relogin.status_code == 401


async def test_viewer_is_forbidden_from_admin_routes(
    client: AsyncClient, register: Register, login: Login, add_member: AddMember
) -> None:
    owner = await register("ada@example.com")
    await add_member("val@example.com", owner["organization_id"], "viewer")

    viewer_token = await login("val@example.com")
    forbidden = await client.get("/api/v1/admin/members", headers=bearer(viewer_token))
    assert forbidden.status_code == 403

    owner_token = await login("ada@example.com")
    allowed = await client.get("/api/v1/admin/members", headers=bearer(owner_token))
    assert allowed.status_code == 200
    assert {(m["email"], m["role"]) for m in allowed.json()} == {
        ("ada@example.com", "owner"),
        ("val@example.com", "viewer"),
    }


async def test_member_list_never_includes_other_organizations(
    client: AsyncClient, register: Register, login: Login
) -> None:
    await register("ada@example.com", org="Org A")
    await register("bob@example.com", org="Org B")
    token = await login("ada@example.com")
    response = await client.get("/api/v1/admin/members", headers=bearer(token))
    assert [m["email"] for m in response.json()] == ["ada@example.com"]


async def test_failed_logins_are_rate_limited(client: AsyncClient, register: Register) -> None:
    await register("ada@example.com")
    for _ in range(3):  # login_max_failures is 3 in the test settings
        wrong = await client.post(
            "/api/v1/auth/login", json={"email": "ada@example.com", "password": "wrong password!"}
        )
        assert wrong.status_code == 401

    # Blocked now, even with the right password.
    blocked = await client.post(
        "/api/v1/auth/login", json={"email": "ada@example.com", "password": PASSWORD}
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


async def test_successful_login_resets_the_failure_count(
    client: AsyncClient, register: Register, login: Login
) -> None:
    await register("ada@example.com")
    for _ in range(2):
        for _ in range(2):
            await client.post(
                "/api/v1/auth/login",
                json={"email": "ada@example.com", "password": "wrong password!"},
            )
        await login("ada@example.com")  # asserts 200


async def test_logins_are_audited(
    client: AsyncClient,
    register: Register,
    login: Login,
    db: async_sessionmaker[AsyncSession],
) -> None:
    await register("ada@example.com")
    await client.post(
        "/api/v1/auth/login", json={"email": "ada@example.com", "password": "wrong password!"}
    )
    await login("ada@example.com")

    async with db() as session:
        events = (
            await session.execute(
                select(AuditEvent.action, AuditEvent.ip).order_by(AuditEvent.created_at)
            )
        ).all()
    assert [action for action, _ in events] == [
        "auth.registered",
        "auth.login_failed",
        "auth.login_succeeded",
    ]
    assert all(str(ip) == "203.0.113.7" for _, ip in events)


async def test_registrations_are_rate_limited_per_ip(client: AsyncClient) -> None:
    statuses = []
    for n in range(6):  # registrations_per_ip_per_hour is 5 in the test settings
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": f"user{n}@example.com", "password": PASSWORD, "organization_name": "X"},
        )
        statuses.append(response.status_code)
    assert statuses == [201] * 5 + [429]
