"""Collections, documents, chunks with vector and full-text indexes; document permissions.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSION = 384

PERMISSIONS_BEFORE: dict[str, list[str]] = {
    "owner": ["org:manage", "members:read", "members:write", "audit:read"],
    "admin": ["members:read", "members:write", "audit:read"],
    "analyst": [],
    "viewer": [],
}
PERMISSIONS_AFTER: dict[str, list[str]] = {
    "owner": PERMISSIONS_BEFORE["owner"] + ["documents:read", "documents:write"],
    "admin": PERMISSIONS_BEFORE["admin"] + ["documents:read", "documents:write"],
    "analyst": ["documents:read", "documents:write"],
    "viewer": ["documents:read"],
}

document_status = postgresql.ENUM(
    "queued", "processing", "ready", "failed", name="document_status", create_type=False
)


def _timestamp(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _fk(
    column: str, target: str, table: str, ondelete: str, nullable: bool = False
) -> sa.Column[object]:
    referred = target.split(".")[0]
    return sa.Column(
        column,
        sa.Uuid(),
        sa.ForeignKey(target, ondelete=ondelete, name=f"fk_{table}_{column}_{referred}"),
        nullable=nullable,
    )


def _set_permissions(permissions: dict[str, list[str]]) -> None:
    for role, perms in permissions.items():
        op.execute(
            sa.text(
                "UPDATE roles SET permissions = CAST(:perms AS jsonb) WHERE name = :role"
            ).bindparams(perms=json.dumps(perms), role=role)
        )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    document_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "collections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _fk("org_id", "organizations.id", "collections", "CASCADE"),
        sa.Column("name", sa.String(100), nullable=False),
        _fk("created_by", "users.id", "collections", "SET NULL", nullable=True),
        _timestamp("created_at"),
        sa.UniqueConstraint("org_id", "name", name="uq_collections_org_id_name"),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _fk("org_id", "organizations.id", "documents", "CASCADE"),
        _fk("collection_id", "collections.id", "documents", "CASCADE"),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", document_status, nullable=False),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column("error_detail", sa.String(500), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("embedding_model", sa.String(100), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_token", sa.Uuid(), nullable=True),
        _fk("uploaded_by", "users.id", "documents", "SET NULL", nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("collection_id", "sha256", name="uq_documents_collection_id_sha256"),
    )
    op.create_index("ix_documents_org_id", "documents", ["org_id"])
    op.create_index("ix_documents_status_created_at", "documents", ["status", "created_at"])

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _fk("org_id", "organizations.id", "document_chunks", "CASCADE"),
        _fk("collection_id", "collections.id", "document_chunks", "CASCADE"),
        _fk("document_id", "documents.id", "document_chunks", "CASCADE"),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSION), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "document_id", "ordinal", name="uq_document_chunks_document_id_ordinal"
        ),
    )
    op.create_index("ix_document_chunks_collection_id", "document_chunks", ["collection_id"])
    op.create_index(
        "ix_document_chunks_embedding",
        "document_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_document_chunks_tsv", "document_chunks", ["tsv"], postgresql_using="gin")

    _set_permissions(PERMISSIONS_AFTER)


def downgrade() -> None:
    _set_permissions(PERMISSIONS_BEFORE)
    op.drop_table("document_chunks")
    op.drop_table("documents")
    op.drop_table("collections")
    document_status.drop(op.get_bind(), checkfirst=True)
    # The vector extension is left installed: other databases objects may use it.
