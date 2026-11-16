"""The ingestion job: claim a queued document, parse, chunk, embed, store.

Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED``, so several workers can run
side by side and never take the same document. Each claim gets a fresh
``lock_token``; every later write checks it. If a worker goes quiet for longer
than ``ingestion_stale_after_seconds``, another worker may take the job over,
and the first worker's late results are then discarded instead of overwriting
the second's.

Results are written in one transaction: the old chunks (from an earlier attempt)
are deleted, the new ones inserted and the document marked ready together, so a
reader never sees a half-indexed document.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import structlog
from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.database.documents import Document, DocumentChunk, DocumentStatus
from backend.ingestion.chunking import Chunk, chunk_pages
from backend.ingestion.detect import FileKind
from backend.ingestion.embedding import Embedder
from backend.ingestion.parsing import ParseError
from backend.ingestion.sandbox import parse_in_sandbox
from backend.ingestion.storage import FileStorage

log = structlog.get_logger("eia.ingestion")

EMBED_BATCH_SIZE = 32


@dataclass(frozen=True)
class Job:
    document_id: uuid.UUID
    org_id: uuid.UUID
    collection_id: uuid.UUID
    kind: FileKind
    attempts: int
    token: uuid.UUID


class LostLockError(Exception):
    """Another worker took this job over; stop without writing anything."""


async def claim_next(session: AsyncSession, settings: Settings) -> Job | None:
    stale_before = datetime.now(UTC) - timedelta(seconds=settings.ingestion_stale_after_seconds)
    next_id = (
        select(Document.id)
        .where(
            or_(
                Document.status == DocumentStatus.QUEUED,
                and_(
                    Document.status == DocumentStatus.PROCESSING,
                    Document.locked_at < stale_before,
                ),
            )
        )
        .order_by(Document.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    token = uuid.uuid4()
    row = (
        await session.execute(
            update(Document)
            .where(Document.id == next_id)
            .values(
                status=DocumentStatus.PROCESSING,
                locked_at=func.now(),
                lock_token=token,
                attempts=Document.attempts + 1,
            )
            .returning(
                Document.id,
                Document.org_id,
                Document.collection_id,
                Document.kind,
                Document.attempts,
            )
        )
    ).one_or_none()
    await session.commit()
    if row is None:
        return None
    document_id, org_id, collection_id, kind, attempts = row
    # kind was validated by detect_kind() at upload time.
    return Job(document_id, org_id, collection_id, cast(FileKind, kind), attempts, token)


class IngestionPipeline:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        storage: FileStorage,
        embedder: Embedder,
        settings: Settings,
    ) -> None:
        self._sessions = sessionmaker
        self._storage = storage
        self._embedder = embedder
        self._settings = settings

    async def run_once(self) -> bool:
        """Claim and process one document. False when the queue is empty."""
        async with self._sessions() as session:
            job = await claim_next(session, self._settings)
        if job is None:
            return False
        structlog.contextvars.bind_contextvars(document_id=str(job.document_id))
        try:
            await self.process(job)
        finally:
            structlog.contextvars.unbind_contextvars("document_id")
        return True

    async def process(self, job: Job) -> None:
        settings = self._settings
        if job.attempts > settings.ingestion_max_attempts:
            await self._fail(job, "too_many_attempts", "Processing failed repeatedly.")
            return
        log.info("ingestion_started", attempt=job.attempts, kind=job.kind)
        try:
            pages = await parse_in_sandbox(
                self._storage.path_for(job.org_id, job.document_id),
                job.kind,
                max_pdf_pages=settings.max_pdf_pages,
                timeout_seconds=settings.parse_timeout_seconds,
                memory_mb=settings.parse_memory_mb,
            )
            chunks = chunk_pages(pages)
            vectors = await self._embed(job, chunks)
            page_count = len(pages) if job.kind == "pdf" else None
            await self._finish(job, chunks, vectors, page_count)
        except ParseError as exc:
            log.info("ingestion_rejected", code=exc.code)
            await self._fail(job, exc.code, exc.detail)
        except LostLockError:
            log.warning("ingestion_lock_lost")
        except FileNotFoundError:
            await self._fail(job, "file_missing", "The uploaded file is no longer available.")
        except Exception:
            # An unexpected error: retry later, unless attempts are used up.
            log.exception("ingestion_error", attempt=job.attempts)
            if job.attempts >= settings.ingestion_max_attempts:
                await self._fail(job, "internal_error", "Processing failed. Try uploading again.")
            else:
                await self._release(job)

    async def _embed(self, job: Job, chunks: list[Chunk]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = [c.text for c in chunks[start : start + EMBED_BATCH_SIZE]]
            # Embedding is CPU-bound and synchronous; keep it off the event loop.
            vectors.extend(await asyncio.to_thread(self._embedder.embed_passages, batch))
            await self._heartbeat(job)
        return vectors

    async def _heartbeat(self, job: Job) -> None:
        async with self._sessions() as session:
            result = await session.execute(
                update(Document)
                .where(Document.id == job.document_id, Document.lock_token == job.token)
                .values(locked_at=func.now())
            )
            await session.commit()
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise LostLockError

    async def _locked_document(self, session: AsyncSession, job: Job) -> Document | None:
        return await session.scalar(
            select(Document)
            .where(Document.id == job.document_id, Document.lock_token == job.token)
            .with_for_update()
        )

    async def _finish(
        self,
        job: Job,
        chunks: list[Chunk],
        vectors: list[list[float]],
        page_count: int | None,
    ) -> None:
        async with self._sessions() as session:
            document = await self._locked_document(session, job)
            if document is None:  # deleted meanwhile, or taken over by another worker
                raise LostLockError
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id == job.document_id)
            )
            if chunks:
                await session.execute(
                    insert(DocumentChunk),
                    [
                        {
                            "id": uuid.uuid4(),
                            "org_id": job.org_id,
                            "collection_id": job.collection_id,
                            "document_id": job.document_id,
                            "ordinal": chunk.ordinal,
                            "page": chunk.page,
                            "char_start": chunk.char_start,
                            "char_end": chunk.char_end,
                            "text": chunk.text,
                            "embedding": vector,
                        }
                        for chunk, vector in zip(chunks, vectors, strict=True)
                    ],
                )
            document.status = DocumentStatus.READY
            document.page_count = page_count
            document.chunk_count = len(chunks)
            document.embedding_model = self._embedder.name
            document.error_code = document.error_detail = None
            document.processed_at = datetime.now(UTC)
            document.lock_token = document.locked_at = None
            await session.commit()
        log.info("ingestion_completed", chunks=len(chunks), pages=page_count)

    async def _fail(self, job: Job, code: str, detail: str) -> None:
        async with self._sessions() as session:
            document = await self._locked_document(session, job)
            if document is None:
                return
            document.status = DocumentStatus.FAILED
            document.error_code = code
            document.error_detail = detail[:500]
            document.processed_at = datetime.now(UTC)
            document.lock_token = document.locked_at = None
            await session.commit()

    async def _release(self, job: Job) -> None:
        async with self._sessions() as session:
            document = await self._locked_document(session, job)
            if document is None:
                return
            document.status = DocumentStatus.QUEUED
            document.lock_token = document.locked_at = None
            await session.commit()
