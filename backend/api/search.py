"""Search endpoint: hybrid retrieval over your organization's collections."""

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.database.documents import Collection
from backend.database.session import get_session
from backend.ingestion.embedding import Embedder, build_embedder
from backend.retrieval.rerank import Reranker, build_reranker
from backend.retrieval.search import Retriever, SearchMode
from backend.security.dependencies import Principal, get_settings_from_app, require_permission

router = APIRouter(tags=["search"])


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=1000)
    collection_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    top_k: int = Field(default=10, ge=1, le=50)
    mode: SearchMode = "hybrid"
    rerank: bool = True


class Hit(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    page: int | None
    char_start: int
    char_end: int
    text: str
    score: float
    vector_rank: int | None
    keyword_rank: int | None
    rerank_score: float | None


class SearchResponse(BaseModel):
    results: list[Hit]
    timings_ms: dict[str, float]


class RetrievalModels:
    """Loads the embedding model and reranker once, on first use, then shares them."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._loaded: tuple[Embedder, Reranker | None] | None = None

    async def get(self) -> tuple[Embedder, Reranker | None]:
        if self._loaded is None:
            async with self._lock:
                if self._loaded is None:
                    embedder = await asyncio.to_thread(build_embedder, self._settings)
                    reranker = await asyncio.to_thread(build_reranker, self._settings)
                    self._loaded = (embedder, reranker)
        return self._loaded


def get_retrieval_models(
    request: Request, settings: Annotated[Settings, Depends(get_settings_from_app)]
) -> RetrievalModels:
    state = request.app.state
    if getattr(state, "retrieval_models", None) is None:
        state.retrieval_models = RetrievalModels(settings)
    models: RetrievalModels = state.retrieval_models
    return models


@router.post("/search", responses={404: {"description": "A collection was not found"}})
async def search(
    body: SearchRequest,
    principal: Annotated[Principal, Depends(require_permission("documents:read"))],
    session: Annotated[AsyncSession, Depends(get_session)],
    models: Annotated[RetrievalModels, Depends(get_retrieval_models)],
) -> SearchResponse:
    requested = set(body.collection_ids)
    owned = await session.scalar(
        select(func.count())
        .select_from(Collection)
        .where(Collection.id.in_(requested), Collection.org_id == principal.org_id)
    )
    if owned != len(requested):
        # Same answer whether the collection is missing or belongs to someone else.
        raise HTTPException(404, "One or more collections were not found.")

    embedder, reranker = await models.get()
    result = await Retriever(session, embedder, reranker).search(
        org_id=principal.org_id,
        collection_ids=sorted(requested),
        query=body.query,
        top_k=body.top_k,
        mode=body.mode,
        rerank=body.rerank,
    )
    return SearchResponse(
        results=[Hit(**hit.__dict__) for hit in result.hits], timings_ms=result.timings_ms
    )
