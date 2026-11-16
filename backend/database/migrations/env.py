"""Alembic environment, async edition.

The URL comes from ``config.attributes["database_url"]`` when a caller (such as
the test suite) provides one, otherwise from ``EIA_DATABASE_URL``. It is not read
through ``Settings`` so migrations do not need the JWT secret or Redis URL.
"""

import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from backend.database import documents, models, tasks  # noqa: F401  (registers the tables)
from backend.database.base import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = config.attributes.get("database_url") or os.environ.get("EIA_DATABASE_URL")
    if not url:
        raise RuntimeError("Set EIA_DATABASE_URL to run migrations.")
    return str(url)


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
