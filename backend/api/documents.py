"""Collections and documents.

Every query is scoped to the caller's organization, taken from the verified
token. A collection or document in another organization is reported as 404, the
same as one that does not exist.

Uploads return 202: the file is stored and queued, and the worker parses and
indexes it in the background. Poll ``GET /documents/{id}`` for the outcome.
"""

import asyncio
import re
import unicodedata
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from backend.api.errors import problem_response
from backend.config import Settings
from backend.database.documents import Collection, Document, DocumentStatus
from backend.database.session import get_session
from backend.ingestion.detect import detect_kind
from backend.ingestion.storage import FileStorage, FileTooLargeError
from backend.security.audit import record_event
from backend.security.dependencies import Principal, get_settings_from_app, require_permission

router = APIRouter(tags=["documents"])

Reader = Annotated[Principal, Depends(require_permission("documents:read"))]
Writer = Annotated[Principal, Depends(require_permission("documents:write"))]
Session = Annotated[AsyncSession, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(get_settings_from_app)]

READ_CHUNK_BYTES = 1024 * 1024
# Room for multipart boundaries and headers around the file itself.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


class CollectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)


class CollectionOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    collection_id: uuid.UUID
    filename: str
    kind: str
    size_bytes: int
    status: DocumentStatus
    error_code: str | None
    error_detail: str | None
    page_count: int | None
    chunk_count: int | None
    created_at: datetime
    processed_at: datetime | None


def get_storage(settings: AppSettings) -> FileStorage:
    return FileStorage(settings.storage_dir)


Storage = Annotated[FileStorage, Depends(get_storage)]


def _not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found.")


async def _collection(
    session: AsyncSession, org_id: uuid.UUID, collection_id: uuid.UUID
) -> Collection:
    found = await session.scalar(
        select(Collection).where(Collection.id == collection_id, Collection.org_id == org_id)
    )
    if found is None:
        raise _not_found("Collection")
    return found


def safe_filename(name: str | None) -> str:
    """A display name: no directories, no control characters, at most 255 characters."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:255] or "upload"


@router.post("/collections", status_code=status.HTTP_201_CREATED)
async def create_collection(
    body: CollectionIn, principal: Writer, session: Session
) -> CollectionOut:
    collection = Collection(
        id=uuid.uuid4(), org_id=principal.org_id, name=body.name, created_by=principal.user_id
    )
    session.add(collection)
    try:
        await session.commit()
    except IntegrityError:
        raise HTTPException(409, "A collection with this name already exists.") from None
    await session.refresh(collection)
    return CollectionOut(id=collection.id, name=collection.name, created_at=collection.created_at)


@router.get("/collections")
async def list_collections(principal: Reader, session: Session) -> list[CollectionOut]:
    rows = await session.scalars(
        select(Collection).where(Collection.org_id == principal.org_id).order_by(Collection.name)
    )
    return [CollectionOut(id=c.id, name=c.name, created_at=c.created_at) for c in rows]


@router.get("/collections/{collection_id}/documents")
async def list_documents(
    collection_id: uuid.UUID, principal: Reader, session: Session
) -> list[DocumentOut]:
    await _collection(session, principal.org_id, collection_id)
    rows = await session.scalars(
        select(Document)
        .where(Document.collection_id == collection_id, Document.org_id == principal.org_id)
        .order_by(Document.created_at.desc())
    )
    return [DocumentOut.model_validate(d) for d in rows]


async def _iter_upload(upload: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await upload.read(READ_CHUNK_BYTES):
        yield chunk


@router.post(
    "/collections/{collection_id}/documents",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DocumentOut,
    responses={
        409: {"description": "This file is already in the collection"},
        411: {"description": "Content-Length header missing"},
        413: {"description": "File larger than the upload limit"},
        415: {"description": "Not a PDF, DOCX or UTF-8 text file"},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
)
async def upload_document(
    collection_id: uuid.UUID,
    request: Request,
    principal: Writer,
    session: Session,
    settings: AppSettings,
    storage: Storage,
) -> Response:
    await _collection(session, principal.org_id, collection_id)

    # Refuse oversized bodies before reading them. The multipart parser would
    # otherwise buffer the whole body to disk first.
    declared = request.headers.get("content-length")
    if declared is None or not declared.isdigit():
        raise HTTPException(411, "A Content-Length header is required for uploads.")
    if int(declared) > settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES:
        raise HTTPException(413, f"Files may be at most {settings.max_upload_bytes} bytes.")

    form = await request.form(max_files=1, max_fields=1)
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(422, "Send the file in a multipart field named 'file'.")
        filename = safe_filename(upload.filename)
        try:
            stored = await storage.save_stream(
                _iter_upload(upload), principal.org_id, settings.max_upload_bytes
            )
        except FileTooLargeError:
            raise HTTPException(
                413, f"Files may be at most {settings.max_upload_bytes} bytes."
            ) from None
    finally:
        await form.close()

    if stored.size_bytes == 0:
        await storage.delete(stored.path)
        raise HTTPException(400, "The file is empty.")
    kind = await asyncio.to_thread(detect_kind, stored.path)
    if kind is None:
        await storage.delete(stored.path)
        raise HTTPException(415, "Only PDF, DOCX and UTF-8 text or Markdown files are supported.")

    existing = await session.scalar(
        select(Document.id).where(
            Document.collection_id == collection_id, Document.sha256 == stored.sha256
        )
    )
    if existing is not None:
        await storage.delete(stored.path)
        return _duplicate(existing)

    document = Document(
        id=uuid.uuid4(),
        org_id=principal.org_id,
        collection_id=collection_id,
        filename=filename,
        kind=kind,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        status=DocumentStatus.QUEUED,
        uploaded_by=principal.user_id,
    )
    final_path = await storage.commit(stored, principal.org_id, document.id)
    session.add(document)
    record_event(
        session,
        "document.uploaded",
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        resource_type="document",
        resource_id=str(document.id),
        details={"kind": kind, "size_bytes": stored.size_bytes},
    )
    try:
        await session.commit()
    except IntegrityError:
        # The same file was uploaded concurrently and the other request won.
        await session.rollback()
        await storage.delete(final_path)
        winner = await session.scalar(
            select(Document.id).where(
                Document.collection_id == collection_id, Document.sha256 == stored.sha256
            )
        )
        if winner is None:
            raise
        return _duplicate(winner)
    except BaseException:
        # Any other failure: no row points at the file, so do not leave it behind.
        await storage.delete(final_path)
        raise

    await session.refresh(document)
    body = DocumentOut.model_validate(document).model_dump(mode="json")
    return JSONResponse(body, status_code=status.HTTP_202_ACCEPTED)


def _duplicate(document_id: uuid.UUID) -> JSONResponse:
    return problem_response(
        409,
        "This file is already in the collection.",
        extra={"document_id": str(document_id)},
    )


@router.get("/documents/{document_id}")
async def get_document(document_id: uuid.UUID, principal: Reader, session: Session) -> DocumentOut:
    document = await session.scalar(
        select(Document).where(Document.id == document_id, Document.org_id == principal.org_id)
    )
    if document is None:
        raise _not_found("Document")
    return DocumentOut.model_validate(document)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID, principal: Writer, session: Session, storage: Storage
) -> Response:
    document = await session.scalar(
        select(Document).where(Document.id == document_id, Document.org_id == principal.org_id)
    )
    if document is None:
        raise _not_found("Document")
    await session.delete(document)  # chunks go with it (ON DELETE CASCADE)
    record_event(
        session,
        "document.deleted",
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        resource_type="document",
        resource_id=str(document_id),
    )
    await session.commit()
    await storage.delete(storage.path_for(principal.org_id, document_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
