"""Fixtures for tests that run against real PostgreSQL and Redis.

Set EIA_DATABASE_URL and EIA_REDIS_URL to run them; otherwise they are skipped.
The suite drops and recreates every table, so it refuses to run unless the
database name ends in ``_test``.
"""

import os
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.database.models import Membership, Role, User
from backend.ingestion.embedding import HashingEmbedder
from backend.ingestion.pipeline import IngestionPipeline
from backend.ingestion.storage import FileStorage
from backend.main import create_app
from backend.security.passwords import hash_password

ROOT = Path(__file__).resolve().parents[2]
# Generated per run, so no secret-shaped literal sits in the repository.
TEST_SECRET = secrets.token_urlsafe(48)
CLIENT_IP = "203.0.113.7"
PASSWORD = "correct horse battery staple"

_DATABASE_URL = os.getenv("EIA_DATABASE_URL")
_REDIS_URL = os.getenv("EIA_REDIS_URL")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if _DATABASE_URL and _REDIS_URL:
        return
    skip = pytest.mark.skip(reason="EIA_DATABASE_URL and EIA_REDIS_URL are not set")
    for item in items:
        if Path(str(item.fspath)).is_relative_to(ROOT / "tests" / "integration"):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def database_url() -> str:
    assert _DATABASE_URL
    name = make_url(_DATABASE_URL).database or ""
    if not name.endswith("_test"):
        pytest.exit(
            f"Refusing to run: integration tests wipe the database, and {name!r} "
            "does not end in '_test'.",
            returncode=2,
        )
    return _DATABASE_URL


@pytest.fixture(scope="session")
def migrated(database_url: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/database/migrations"))
    config.attributes["database_url"] = database_url
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest.fixture
def settings(database_url: str, tmp_path: Path) -> Settings:
    assert _REDIS_URL
    return Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
        redis_url=_REDIS_URL,
        jwt_secret=TEST_SECRET,
        login_max_failures=3,
        registrations_per_ip_per_hour=5,
        readiness_timeout_seconds=1,
        storage_dir=tmp_path / "uploads",
        max_upload_bytes=2 * 1024 * 1024,
        embedding_backend="hashing",
        reranker_backend="none",
        parse_timeout_seconds=60,
    )


@pytest.fixture
async def app(settings: Settings, migrated: None) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        async with application.state.engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE document_chunks, documents, collections, audit_events, "
                    "refresh_tokens, memberships, users, organizations "
                    "RESTART IDENTITY CASCADE"
                )
            )
        redis = application.state.redis
        async for key in redis.scan_iter("rl:*"):
            await redis.delete(key)
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    # https so the client sends the Secure refresh cookie back.
    transport = ASGITransport(app=app, client=(CLIENT_IP, 40000))
    async with AsyncClient(transport=transport, base_url="https://test") as c:
        yield c


@pytest.fixture
def db(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    sessionmaker: async_sessionmaker[AsyncSession] = app.state.sessionmaker
    return sessionmaker


Register = Callable[..., Awaitable[dict[str, str]]]


@pytest.fixture
def register(client: AsyncClient) -> Register:
    async def _register(email: str, org: str = "Acme Research") -> dict[str, str]:
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": PASSWORD, "organization_name": org},
        )
        assert response.status_code == 201, response.text
        body: dict[str, str] = response.json()
        return body

    return _register


Login = Callable[..., Awaitable[str]]


@pytest.fixture
def login(client: AsyncClient) -> Login:
    async def _login(email: str, password: str = PASSWORD) -> str:
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        assert response.status_code == 200, response.text
        token: str = response.json()["access_token"]
        return token

    return _login


AddMember = Callable[..., Awaitable[None]]


@pytest.fixture
def add_member(db: async_sessionmaker[AsyncSession]) -> AddMember:
    """Insert a user into an existing organization with a given role.

    M1 has no invitation endpoint yet, so tests create memberships directly.
    """

    async def _add(email: str, org_id: str, role: str) -> None:
        async with db() as session:
            role_id = await session.scalar(select(Role.id).where(Role.name == role))
            user = User(id=uuid.uuid4(), email=email, password_hash=await hash_password(PASSWORD))
            session.add(user)
            await session.flush()
            session.add(Membership(user_id=user.id, org_id=uuid.UUID(org_id), role_id=role_id))
            await session.commit()

    return _add


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def pipeline(app: FastAPI, settings: Settings) -> IngestionPipeline:
    return IngestionPipeline(
        app.state.sessionmaker, FileStorage(settings.storage_dir), HashingEmbedder(), settings
    )


async def drain(pipeline: IngestionPipeline) -> int:
    """Run the worker until the queue is empty; return how many jobs it handled."""
    handled = 0
    while await pipeline.run_once():
        handled += 1
    return handled
