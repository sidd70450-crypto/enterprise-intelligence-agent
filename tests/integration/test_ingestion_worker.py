"""How the ingestion worker behaves when things go wrong."""

import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.database.documents import Document, DocumentStatus
from backend.ingestion.embedding import HashingEmbedder
from backend.ingestion.pipeline import IngestionPipeline, claim_next
from backend.ingestion.storage import FileStorage
from tests.integration.conftest import Login, Register, bearer, drain


class FlakyEmbedder(HashingEmbedder):
    """Fails the first ``failures`` calls, then works."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("model server hiccup")
        return super().embed_passages(texts)


@pytest.fixture
async def queued(client: AsyncClient, register: Register, login: Login) -> list[str]:
    """Two queued text documents; returns their IDs."""
    await register("ada@example.com")
    token = await login("ada@example.com")
    collection = await client.post("/api/v1/collections", json={"name": "C"}, headers=bearer(token))
    ids = []
    for n in range(2):
        response = await client.post(
            f"/api/v1/collections/{collection.json()['id']}/documents",
            files={"file": (f"{n}.txt", f"Document number {n} with some text.\n".encode())},
            headers=bearer(token),
        )
        ids.append(response.json()["id"])
    return ids


async def _status(db: async_sessionmaker[AsyncSession], document_id: str) -> Document:
    async with db() as session:
        document = await session.get(Document, uuid.UUID(document_id))
    assert document is not None
    return document


def _pipeline(
    db: async_sessionmaker[AsyncSession], settings: Settings, embedder: HashingEmbedder
) -> IngestionPipeline:
    return IngestionPipeline(db, FileStorage(settings.storage_dir), embedder, settings)


async def test_a_transient_error_is_retried(
    queued: list[str], db: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    pipeline = _pipeline(db, settings, FlakyEmbedder(failures=1))
    await drain(pipeline)
    documents = [await _status(db, i) for i in queued]
    assert [d.status for d in documents] == [DocumentStatus.READY, DocumentStatus.READY]
    assert sorted(d.attempts for d in documents) == [1, 2]


async def test_a_persistent_error_fails_after_the_last_attempt(
    queued: list[str], db: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    pipeline = _pipeline(db, settings, FlakyEmbedder(failures=1000))
    await drain(pipeline)
    for document_id in queued:
        document = await _status(db, document_id)
        assert (document.status, document.error_code) == (DocumentStatus.FAILED, "internal_error")
        assert document.attempts == settings.ingestion_max_attempts
        assert "hiccup" not in (document.error_detail or "")  # internals stay in the logs


async def test_two_workers_never_claim_the_same_document(
    queued: list[str], db: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    async def claim() -> uuid.UUID | None:
        async with db() as session:
            job = await claim_next(session, settings)
        return job.document_id if job else None

    claimed = await asyncio.gather(claim(), claim(), claim())
    found = [c for c in claimed if c is not None]
    assert sorted(map(str, found)) == sorted(queued)  # each exactly once, third gets nothing
    assert claimed.count(None) == 1


async def test_a_job_abandoned_by_a_crashed_worker_is_taken_over(
    queued: list[str], db: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    async with db() as session:
        crashed = await claim_next(session, settings)
        assert crashed is not None
        # The worker died: no heartbeat for longer than the stale limit.
        await session.execute(
            update(Document)
            .where(Document.id == crashed.document_id)
            .values(
                locked_at=datetime.now(UTC)
                - timedelta(seconds=settings.ingestion_stale_after_seconds + 1)
            )
        )
        await session.commit()

    await drain(_pipeline(db, settings, HashingEmbedder()))
    document = await _status(db, str(crashed.document_id))
    assert document.status == DocumentStatus.READY
    assert document.attempts == 2


async def test_a_slow_worker_that_lost_its_job_cannot_overwrite_results(
    queued: list[str], db: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    async with db() as session:
        slow = await claim_next(session, settings)
        assert slow is not None
        # Another worker takes the job over (simulated by replacing the lock token).
        await session.execute(
            update(Document).where(Document.id == slow.document_id).values(lock_token=uuid.uuid4())
        )
        await session.commit()

    await _pipeline(db, settings, HashingEmbedder()).process(slow)
    document = await _status(db, str(slow.document_id))
    assert document.status == DocumentStatus.PROCESSING  # untouched by the slow worker
    assert document.chunk_count is None


async def test_a_document_deleted_mid_processing_is_skipped_cleanly(
    client: AsyncClient,
    queued: list[str],
    db: async_sessionmaker[AsyncSession],
    settings: Settings,
    login: Login,
) -> None:
    async with db() as session:
        job = await claim_next(session, settings)
    assert job is not None
    token = await login("ada@example.com")
    await client.delete(f"/api/v1/documents/{job.document_id}", headers=bearer(token))

    await _pipeline(db, settings, HashingEmbedder()).process(job)  # must not raise
    async with db() as session:
        assert await session.get(Document, job.document_id) is None


async def test_a_job_over_its_attempt_limit_is_failed_without_processing(
    queued: list[str], db: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    async with db() as session:
        await session.execute(update(Document).values(attempts=settings.ingestion_max_attempts))
        await session.commit()
    await drain(_pipeline(db, settings, HashingEmbedder()))
    async with db() as session:
        codes = (await session.scalars(select(Document.error_code))).all()
    assert codes == ["too_many_attempts", "too_many_attempts"]
