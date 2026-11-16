"""Collections, documents and their chunks (milestone M2).

A document row doubles as its own ingestion job: the worker claims rows in
``queued`` status with ``SELECT ... FOR UPDATE SKIP LOCKED``. Because the job is
the row, an upload and its job are created in one transaction and can never get
out of step. See docs/adr/0004-document-ingestion.md.
"""

import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import models as _identity_models  # noqa: F401
from backend.database.base import Base, created_at, uuid_pk

# The import above registers organizations and users, which these tables
# reference. Without it, a process that imports only this module (the worker)
# cannot resolve the foreign keys and every write fails.

# Fixed by the embedding model (BAAI/bge-small-en-v1.5). Changing models with a
# different size needs a migration and re-embedding every chunk.
EMBEDDING_DIMENSION = 384


class DocumentStatus(enum.StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class Collection(Base):
    __tablename__ = "collections"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at()


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        # The same file can live in two collections, but not twice in one.
        UniqueConstraint("collection_id", "sha256"),
        Index("ix_documents_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE")
    )
    # The name the user uploaded, for display only. Files are stored by ID.
    filename: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))  # pdf | docx | text, detected from content
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            name="document_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=DocumentStatus.QUEUED,
    )
    error_code: Mapped[str | None] = mapped_column(String(32))
    error_detail: Mapped[str | None] = mapped_column(String(500))
    page_count: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    # Job bookkeeping. lock_token identifies the worker that holds the job, so a
    # worker whose job was taken over after going quiet cannot overwrite results.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_token: Mapped[uuid.UUID | None]
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentChunk(Base):
    """A passage of one page, with its exact position in that page's text.

    ``text == page_text[char_start:char_end]`` holds for every chunk, so a quote
    found inside a chunk can be traced back to its page and offset.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal"),
        Index("ix_document_chunks_collection_id", "collection_id"),
        Index(
            "ix_document_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_document_chunks_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    # Copied from the document so retrieval can filter without a join.
    collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)
    page: Mapped[int | None] = mapped_column(Integer)  # 1-based; None for formats without pages
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION))
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True)
    )
