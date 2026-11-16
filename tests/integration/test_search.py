"""Hybrid search through the API and the Retriever, against a real database."""

import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.ingestion.embedding import HashingEmbedder
from backend.ingestion.pipeline import IngestionPipeline
from backend.retrieval.search import Retriever
from tests.integration.conftest import AddMember, Login, Register, bearer, drain

DOCUMENTS = {
    "retention.md": (
        "# Data retention policy POL-114\n\n"
        "Customer access logs are kept for 400 days and then deleted automatically."
    ),
    "runbook.md": (
        "# Payments runbook\n\n"
        "Error E4021 means the card network timed out. Retry once, then escalate."
    ),
    "travel.md": (
        "# Travel policy\n\nEmployees book economy class for flights shorter than six hours."
    ),
}


async def _index(
    client: AsyncClient, token: str, pipeline: IngestionPipeline, docs: dict[str, str]
) -> str:
    collection = await client.post(
        "/api/v1/collections", json={"name": "Docs"}, headers=bearer(token)
    )
    collection_id: str = collection.json()["id"]
    for name, content in docs.items():
        response = await client.post(
            f"/api/v1/collections/{collection_id}/documents",
            files={"file": (name, content.encode())},
            headers=bearer(token),
        )
        assert response.status_code == 202, response.text
    await drain(pipeline)
    return collection_id


@pytest.fixture
async def indexed(
    client: AsyncClient, register: Register, login: Login, pipeline: IngestionPipeline
) -> dict[str, str]:
    account = await register("ada@example.com", org="Org A")
    token = await login("ada@example.com")
    collection_id = await _index(client, token, pipeline, DOCUMENTS)
    return {**account, "token": token, "collection_id": collection_id}


async def _search(client: AsyncClient, token: str, **body: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/search", json=body, headers=bearer(token))
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


@pytest.mark.parametrize("mode", ["keyword", "vector", "hybrid"])
async def test_an_exact_identifier_is_found_in_every_mode(
    client: AsyncClient, indexed: dict[str, str], mode: str
) -> None:
    result = await _search(
        client, indexed["token"], query="What does E4021 mean?",
        collection_ids=[indexed["collection_id"]], mode=mode,
    )  # fmt: skip
    assert result["results"][0]["filename"] == "runbook.md"
    assert mode in result["timings_ms"] or mode == "hybrid"


async def test_a_natural_question_matches_chunks_with_only_some_of_its_words(
    client: AsyncClient, indexed: dict[str, str]
) -> None:
    # No chunk contains all of "how", "long", "customer", "logs", "kept"; OR-ed
    # terms still find the retention policy.
    result = await _search(
        client, indexed["token"], query="How long are customer logs kept?",
        collection_ids=[indexed["collection_id"]], mode="keyword",
    )  # fmt: skip
    assert result["results"][0]["filename"] == "retention.md"


async def test_hybrid_results_carry_both_ranks_and_the_citation_position(
    client: AsyncClient, indexed: dict[str, str]
) -> None:
    result = await _search(
        client, indexed["token"], query="POL-114 retention",
        collection_ids=[indexed["collection_id"]],
    )  # fmt: skip
    top = result["results"][0]
    assert top["filename"] == "retention.md"
    assert top["vector_rank"] == 1 and top["keyword_rank"] == 1
    assert top["char_end"] - top["char_start"] == len(top["text"])
    assert set(result["timings_ms"]) == {"vector", "keyword"}


async def test_top_k_limits_the_results(client: AsyncClient, indexed: dict[str, str]) -> None:
    result = await _search(
        client, indexed["token"], query="policy", collection_ids=[indexed["collection_id"]],
        top_k=1,
    )  # fmt: skip
    assert len(result["results"]) == 1


async def test_search_never_returns_another_organizations_chunks(
    client: AsyncClient,
    indexed: dict[str, str],
    register: Register,
    login: Login,
    pipeline: IngestionPipeline,
) -> None:
    await register("eve@example.com", org="Org E")
    eve = await login("eve@example.com")
    eve_collection = await _index(
        client, eve, pipeline, {"eve.md": "Eve's notes mention error E4021 and POL-114 too."}
    )

    # Eve asks about Org A's identifiers in her own collection: only her file comes back.
    result = await _search(client, eve, query="E4021 POL-114", collection_ids=[eve_collection])
    assert {hit["filename"] for hit in result["results"]} == {"eve.md"}

    # Naming Org A's collection, alone or next to her own, is refused outright.
    for ids in ([indexed["collection_id"]], [eve_collection, indexed["collection_id"]]):
        response = await client.post(
            "/api/v1/search", json={"query": "E4021", "collection_ids": ids}, headers=bearer(eve)
        )
        assert response.status_code == 404


async def test_viewers_can_search(
    client: AsyncClient, indexed: dict[str, str], add_member: AddMember, login: Login
) -> None:
    await add_member("val@example.com", indexed["organization_id"], "viewer")
    viewer = await login("val@example.com")
    result = await _search(
        client, viewer, query="economy flights", collection_ids=[indexed["collection_id"]]
    )
    assert result["results"][0]["filename"] == "travel.md"


async def test_search_requires_a_token(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/search", json={"query": "x", "collection_ids": [str(uuid.uuid4())]}
    )
    assert response.status_code == 401


class ReversingReranker:
    """Scores passages so that the original order is reversed."""

    name = "reversing"

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        return [float(i) for i in range(len(passages))]


async def test_the_reranker_reorders_the_fused_candidates(
    indexed: dict[str, str], db: async_sessionmaker[AsyncSession]
) -> None:
    args = {
        "org_id": uuid.UUID(indexed["organization_id"]),
        "collection_ids": [uuid.UUID(indexed["collection_id"])],
        "query": "policy retention travel payments",
    }
    async with db() as session:
        plain = await Retriever(session, HashingEmbedder()).search(**args)  # type: ignore[arg-type]
    async with db() as session:
        reranked = await Retriever(session, HashingEmbedder(), ReversingReranker()).search(
            **args  # type: ignore[arg-type]
        )
    assert [h.chunk_id for h in reranked.hits] == [h.chunk_id for h in reversed(plain.hits)]
    assert all(h.rerank_score is not None for h in reranked.hits)
    assert "rerank" in reranked.timings_ms
