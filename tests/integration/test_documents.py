"""Upload, ingestion and access control for documents, against a real database."""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.database.documents import DocumentChunk
from backend.ingestion.embedding import HashingEmbedder
from backend.ingestion.pipeline import IngestionPipeline
from tests.files import make_docx, make_pdf, make_zip_bomb
from tests.integration.conftest import AddMember, Login, Register, bearer, drain

PAGES = 50


@pytest.fixture
async def owner(register: Register, login: Login) -> dict[str, str]:
    account = await register("ada@example.com", org="Org A")
    return {**account, "token": await login("ada@example.com")}


async def _collection(client: AsyncClient, token: str, name: str = "Reports") -> str:
    response = await client.post("/api/v1/collections", json={"name": name}, headers=bearer(token))
    assert response.status_code == 201, response.text
    collection_id: str = response.json()["id"]
    return collection_id


async def _upload(
    client: AsyncClient, token: str, collection_id: str, name: str, content: bytes
) -> Response:
    return await client.post(
        f"/api/v1/collections/{collection_id}/documents",
        files={"file": (name, content, "application/octet-stream")},
        headers=bearer(token),
    )


async def _document(client: AsyncClient, token: str, document_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/documents/{document_id}", headers=bearer(token))
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def _report_pages() -> list[str]:
    return [
        f"Page {n} of the annual report.\n"
        f"This page discusses the topic kappa{n:02d}x in detail.\n"
        "Revenue, costs and headcount are summarised for the period."
        for n in range(1, PAGES + 1)
    ]


async def test_a_50_page_pdf_becomes_searchable_by_keyword_and_vector(
    client: AsyncClient,
    owner: dict[str, str],
    pipeline: IngestionPipeline,
    db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    collection_id = await _collection(client, owner["token"])
    pdf = make_pdf(tmp_path / "annual-report.pdf", _report_pages()).read_bytes()

    accepted = await _upload(client, owner["token"], collection_id, "annual-report.pdf", pdf)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["status"] == "queued"

    assert await drain(pipeline) == 1
    document = await _document(client, owner["token"], accepted.json()["id"])
    assert document["status"] == "ready", document
    assert document["kind"] == "pdf"
    assert document["page_count"] == PAGES
    assert document["chunk_count"] >= PAGES

    async with db() as session:
        # Keyword search through the generated tsvector and its GIN index.
        keyword_pages = (
            await session.scalars(
                select(DocumentChunk.page).where(
                    DocumentChunk.tsv.op("@@")(func.plainto_tsquery("english", "kappa37x"))
                )
            )
        ).all()
        # Vector search through the HNSW index.
        query = HashingEmbedder().embed_query("kappa37x")
        nearest = await session.scalar(
            select(DocumentChunk.page)
            .order_by(DocumentChunk.embedding.cosine_distance(query))
            .limit(1)
        )
        chunks = (await session.scalars(select(DocumentChunk))).all()

    assert set(keyword_pages) == {37}
    assert nearest == 37
    assert {c.collection_id for c in chunks} == {uuid.UUID(collection_id)}
    assert all(
        c.text.strip() == c.text and c.char_end - c.char_start == len(c.text) for c in chunks
    )


async def test_corrupt_pdf_fails_with_a_reason(
    client: AsyncClient,
    owner: dict[str, str],
    pipeline: IngestionPipeline,
    db: async_sessionmaker[AsyncSession],
) -> None:
    collection_id = await _collection(client, owner["token"])
    corrupt = b"%PDF-1.7\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n" + b"\x00\xff" * 500
    accepted = await _upload(client, owner["token"], collection_id, "broken.pdf", corrupt)
    assert accepted.status_code == 202

    await drain(pipeline)
    document = await _document(client, owner["token"], accepted.json()["id"])
    assert document["status"] == "failed"
    assert document["error_code"] in {"corrupt", "no_text"}
    assert document["error_detail"]
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(DocumentChunk)) == 0


async def test_docx_and_markdown_are_ingested(
    client: AsyncClient, owner: dict[str, str], pipeline: IngestionPipeline, tmp_path: Path
) -> None:
    collection_id = await _collection(client, owner["token"])
    docx_bytes = make_docx(
        tmp_path / "memo.docx", ["Board memo about the merger."], table=[["Item", "Cost"]]
    ).read_bytes()
    markdown = b"# Findings\n\nThe pilot reduced churn by a measurable amount.\n"
    ids = [
        (await _upload(client, owner["token"], collection_id, "memo.docx", docx_bytes)).json()[
            "id"
        ],
        (await _upload(client, owner["token"], collection_id, "notes.md", markdown)).json()["id"],
    ]
    await drain(pipeline)
    documents = [await _document(client, owner["token"], i) for i in ids]
    assert [(d["kind"], d["status"], d["page_count"]) for d in documents] == [
        ("docx", "ready", None),
        ("text", "ready", None),
    ]


async def test_zip_bomb_is_rejected_by_the_worker(
    client: AsyncClient, owner: dict[str, str], pipeline: IngestionPipeline, tmp_path: Path
) -> None:
    collection_id = await _collection(client, owner["token"])
    bomb = make_zip_bomb(tmp_path / "bomb.docx").read_bytes()
    accepted = await _upload(client, owner["token"], collection_id, "bomb.docx", bomb)
    await drain(pipeline)
    document = await _document(client, owner["token"], accepted.json()["id"])
    assert (document["status"], document["error_code"]) == ("failed", "unsafe_archive")


@pytest.mark.parametrize(
    ("content", "status"),
    [
        (b"MZ\x90\x00\x03\x00\x00\x00\x04\x00", 415),  # an executable named .pdf
        (b"", 400),
    ],
)
async def test_bad_uploads_are_refused_and_leave_no_file(
    client: AsyncClient,
    owner: dict[str, str],
    settings: Settings,
    content: bytes,
    status: int,
) -> None:
    collection_id = await _collection(client, owner["token"])
    response = await _upload(client, owner["token"], collection_id, "invoice.pdf", content)
    assert response.status_code == status
    assert list(settings.storage_dir.rglob("*")) == [
        settings.storage_dir / owner["organization_id"]
    ]


async def test_oversized_upload_is_refused(
    client: AsyncClient, owner: dict[str, str], settings: Settings
) -> None:
    collection_id = await _collection(client, owner["token"])
    too_big = b"a" * (settings.max_upload_bytes + 200 * 1024)
    response = await _upload(client, owner["token"], collection_id, "big.txt", too_big)
    assert response.status_code == 413
    assert not [p for p in settings.storage_dir.rglob("*") if p.is_file()]


async def test_upload_without_content_length_is_refused(
    client: AsyncClient, owner: dict[str, str]
) -> None:
    collection_id = await _collection(client, owner["token"])

    async def body() -> AsyncIterator[bytes]:
        yield b'--x\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'
        yield b"hello\r\n--x--\r\n"

    response = await client.post(
        f"/api/v1/collections/{collection_id}/documents",
        content=body(),
        headers={**bearer(owner["token"]), "content-type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 411


async def test_duplicate_upload_points_to_the_existing_document(
    client: AsyncClient, owner: dict[str, str]
) -> None:
    collection_id = await _collection(client, owner["token"])
    content = b"Exactly the same content twice.\n"
    first = await _upload(client, owner["token"], collection_id, "a.txt", content)
    second = await _upload(client, owner["token"], collection_id, "b.txt", content)
    assert second.status_code == 409
    assert second.json()["document_id"] == first.json()["id"]


async def test_filename_is_sanitized_and_never_used_as_a_path(
    client: AsyncClient, owner: dict[str, str], settings: Settings
) -> None:
    collection_id = await _collection(client, owner["token"])
    response = await _upload(
        client, owner["token"], collection_id, "../../etc/passwd", b"Plain text content.\n"
    )
    assert response.status_code == 202
    assert response.json()["filename"] == "passwd"
    stored = [p for p in settings.storage_dir.rglob("*") if p.is_file()]
    assert stored == [settings.storage_dir / owner["organization_id"] / response.json()["id"]]


async def test_delete_removes_the_document_its_chunks_and_its_file(
    client: AsyncClient,
    owner: dict[str, str],
    pipeline: IngestionPipeline,
    db: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    collection_id = await _collection(client, owner["token"])
    accepted = await _upload(
        client, owner["token"], collection_id, "a.txt", b"Some text worth indexing.\n" * 20
    )
    await drain(pipeline)
    deleted = await client.delete(
        f"/api/v1/documents/{accepted.json()['id']}", headers=bearer(owner["token"])
    )
    assert deleted.status_code == 204
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(DocumentChunk)) == 0
    assert not [p for p in settings.storage_dir.rglob("*") if p.is_file()]


async def test_other_organizations_cannot_see_or_touch_documents(
    client: AsyncClient, owner: dict[str, str], register: Register, login: Login
) -> None:
    collection_id = await _collection(client, owner["token"])
    accepted = await _upload(client, owner["token"], collection_id, "a.txt", b"Org A secret.\n")
    document_id = accepted.json()["id"]

    await register("eve@example.com", org="Org E")
    eve = await login("eve@example.com")
    attempts = [
        await client.get(f"/api/v1/documents/{document_id}", headers=bearer(eve)),
        await client.delete(f"/api/v1/documents/{document_id}", headers=bearer(eve)),
        await client.get(f"/api/v1/collections/{collection_id}/documents", headers=bearer(eve)),
        await _upload(client, eve, collection_id, "x.txt", b"Planted by Eve.\n"),
    ]
    assert [r.status_code for r in attempts] == [404, 404, 404, 404]
    listed = await client.get("/api/v1/collections", headers=bearer(eve))
    assert listed.json() == []


async def test_viewers_can_read_but_not_upload(
    client: AsyncClient, owner: dict[str, str], add_member: AddMember, login: Login
) -> None:
    collection_id = await _collection(client, owner["token"])
    await add_member("val@example.com", owner["organization_id"], "viewer")
    viewer = await login("val@example.com")

    listed = await client.get(
        f"/api/v1/collections/{collection_id}/documents", headers=bearer(viewer)
    )
    assert listed.status_code == 200
    upload = await _upload(client, viewer, collection_id, "a.txt", b"Viewer upload.\n")
    assert upload.status_code == 403
    create = await client.post("/api/v1/collections", json={"name": "X"}, headers=bearer(viewer))
    assert create.status_code == 403


async def test_collection_names_are_unique_per_organization(
    client: AsyncClient, owner: dict[str, str]
) -> None:
    await _collection(client, owner["token"], "Reports")
    again = await client.post(
        "/api/v1/collections", json={"name": "Reports"}, headers=bearer(owner["token"])
    )
    assert again.status_code == 409


async def test_unsupported_but_text_like_content_is_rejected_at_parse_time(
    client: AsyncClient,
    owner: dict[str, str],
    pipeline: IngestionPipeline,
    db: async_sessionmaker[AsyncSession],
) -> None:
    # Passes the upload sniff (valid UTF-8 in the first 8 KB) but is not UTF-8 later.
    content = b"a" * 9000 + "café".encode("latin-1")
    collection_id = await _collection(client, owner["token"])
    accepted = await _upload(client, owner["token"], collection_id, "a.txt", content)
    assert accepted.status_code == 202
    await drain(pipeline)
    document = await _document(client, owner["token"], accepted.json()["id"])
    assert (document["status"], document["error_code"]) == ("failed", "not_utf8")
    async with db() as session:
        uploaded = await session.scalar(
            text("SELECT count(*) FROM audit_events WHERE action = 'document.uploaded'")
        )
    assert uploaded == 1
