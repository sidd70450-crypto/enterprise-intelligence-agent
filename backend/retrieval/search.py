"""Hybrid retrieval: vector search, keyword search, rank fusion and reranking.

1. **Vector search** ranks chunks by cosine distance between the query embedding
   and each chunk embedding (HNSW index). Good at paraphrase: "how much money did
   we make" finds "revenue rose to 4.2 billion".
2. **Keyword search** ranks chunks by PostgreSQL full-text match (GIN index).
   Good at exact terms a model may blur: "POL-114", "E4021". The query's terms
   are OR-ed, so a natural-language question still matches chunks that contain
   only some of its words; ``ts_rank_cd`` rewards chunks matching more of them,
   close together. This is PostgreSQL's ranking, not BM25 (see ADR 0005).
3. **Reciprocal rank fusion** merges the two ranked lists using only ranks, so
   the two incomparable score scales never have to be calibrated against each
   other.
4. **Reranking** (optional) scores the fused top candidates with a cross-encoder,
   which reads the query and passage together and is more accurate but too slow
   to run on every chunk.

Every query is filtered by organization and collection inside the SQL, so
another tenant's chunks are never candidates, not merely filtered out later.
"""

import asyncio
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from sqlalchemy import Select, Text, cast, func, select, text
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.documents import Document, DocumentChunk
from backend.ingestion.embedding import Embedder
from backend.retrieval.rerank import Reranker

SearchMode = Literal["vector", "keyword", "hybrid"]

RRF_K = 60  # the constant from the original RRF paper; dampens the weight of top ranks
DEFAULT_CANDIDATES = 50


@dataclass(frozen=True)
class SearchHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    page: int | None
    char_start: int
    char_end: int
    text: str
    score: float
    vector_rank: int | None = None
    keyword_rank: int | None = None
    rerank_score: float | None = None


@dataclass
class SearchResult:
    hits: list[SearchHit]
    timings_ms: dict[str, float] = field(default_factory=dict)


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[uuid.UUID]], k: int = RRF_K
) -> dict[uuid.UUID, float]:
    """score(d) = sum over lists of 1 / (k + rank of d in that list), ranks from 1."""
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return scores


def _scoped[S: Select[Any]](query: S, org_id: uuid.UUID, collection_ids: Sequence[uuid.UUID]) -> S:
    return query.where(
        DocumentChunk.org_id == org_id, DocumentChunk.collection_id.in_(list(collection_ids))
    )


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


class Retriever:
    def __init__(
        self, session: AsyncSession, embedder: Embedder, reranker: Reranker | None = None
    ) -> None:
        self._session = session
        self._embedder = embedder
        self._reranker = reranker

    async def vector_ranking(
        self, org_id: uuid.UUID, collection_ids: Sequence[uuid.UUID], query: str, limit: int
    ) -> list[uuid.UUID]:
        vector = await asyncio.to_thread(self._embedder.embed_query, query)
        # A wider HNSW search list keeps recall up when the tenant filter removes
        # many of the nearest neighbours. Scoped to this transaction only.
        await self._session.execute(text("SET LOCAL hnsw.ef_search = 100"))
        statement = _scoped(
            select(DocumentChunk.id)
            .order_by(DocumentChunk.embedding.cosine_distance(vector))
            .limit(limit),
            org_id,
            collection_ids,
        )
        return list((await self._session.scalars(statement)).all())

    async def keyword_ranking(
        self, org_id: uuid.UUID, collection_ids: Sequence[uuid.UUID], query: str, limit: int
    ) -> list[uuid.UUID]:
        # plainto_tsquery stems and drops stop words, joining terms with AND;
        # swapping AND for OR turns a question into "any of these words".
        tsquery = cast(
            func.replace(cast(func.plainto_tsquery("english", query), Text), " & ", " | "),
            TSQUERY,
        )
        statement = _scoped(
            select(DocumentChunk.id)
            .where(DocumentChunk.tsv.op("@@")(tsquery))
            .order_by(func.ts_rank_cd(DocumentChunk.tsv, tsquery).desc(), DocumentChunk.id)
            .limit(limit),
            org_id,
            collection_ids,
        )
        return list((await self._session.scalars(statement)).all())

    async def search(
        self,
        *,
        org_id: uuid.UUID,
        collection_ids: Sequence[uuid.UUID],
        query: str,
        top_k: int = 10,
        mode: SearchMode = "hybrid",
        rerank: bool = True,
        candidates: int = DEFAULT_CANDIDATES,
    ) -> SearchResult:
        timings: dict[str, float] = {}

        pool = max(candidates, top_k)
        vector_ids: list[uuid.UUID] = []
        keyword_ids: list[uuid.UUID] = []
        # One session cannot run two statements at once, so the two searches run
        # one after the other.
        if mode in ("vector", "hybrid"):
            started = time.perf_counter()
            vector_ids = await self.vector_ranking(org_id, collection_ids, query, pool)
            timings["vector"] = _ms(started)
        if mode in ("keyword", "hybrid"):
            started = time.perf_counter()
            keyword_ids = await self.keyword_ranking(org_id, collection_ids, query, pool)
            timings["keyword"] = _ms(started)

        fused = reciprocal_rank_fusion([r for r in (vector_ids, keyword_ids) if r])
        ordered = sorted(fused, key=lambda chunk_id: fused[chunk_id], reverse=True)[:pool]
        hits = await self._load(ordered, fused, vector_ids, keyword_ids)

        if rerank and self._reranker is not None and hits:
            started = time.perf_counter()
            scores = await asyncio.to_thread(
                self._reranker.score, query, [hit.text for hit in hits]
            )
            hits = sorted(
                (
                    replace(hit, rerank_score=s, score=s)
                    for hit, s in zip(hits, scores, strict=True)
                ),
                key=lambda hit: hit.score,
                reverse=True,
            )
            timings["rerank"] = _ms(started)

        return SearchResult(hits=hits[:top_k], timings_ms=timings)

    async def _load(
        self,
        ordered: list[uuid.UUID],
        fused: dict[uuid.UUID, float],
        vector_ids: list[uuid.UUID],
        keyword_ids: list[uuid.UUID],
    ) -> list[SearchHit]:
        if not ordered:
            return []
        rows = await self._session.execute(
            select(DocumentChunk, Document.filename)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.id.in_(ordered))
        )
        by_id = {chunk.id: (chunk, filename) for chunk, filename in rows}
        vector_rank = {cid: n for n, cid in enumerate(vector_ids, start=1)}
        keyword_rank = {cid: n for n, cid in enumerate(keyword_ids, start=1)}
        hits = []
        for chunk_id in ordered:
            chunk, filename = by_id[chunk_id]
            hits.append(
                SearchHit(
                    chunk_id=chunk.id,
                    document_id=chunk.document_id,
                    filename=filename,
                    page=chunk.page,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    text=chunk.text,
                    score=fused[chunk_id],
                    vector_rank=vector_rank.get(chunk_id),
                    keyword_rank=keyword_rank.get(chunk_id),
                )
            )
        return hits
