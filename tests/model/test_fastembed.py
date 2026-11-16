"""Checks against the real embedding model.

These download BAAI/bge-small-en-v1.5 (about 70 MB) from Hugging Face, so they
only run when EIA_RUN_MODEL_TESTS=1. CI runs them in their own job.
"""

import math
import os

import pytest

from backend.config import Settings
from backend.ingestion.embedding import build_embedder

pytestmark = pytest.mark.skipif(
    os.getenv("EIA_RUN_MODEL_TESTS") != "1",
    reason="set EIA_RUN_MODEL_TESTS=1 to download the model",
)


@pytest.fixture(scope="module")
def embedder():  # type: ignore[no-untyped-def]
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://unused@localhost/unused",
        redis_url="redis://localhost",
        jwt_secret="x" * 32,
        embedding_backend="fastembed",
    )
    return build_embedder(settings)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def test_model_matches_the_database_column(embedder) -> None:  # type: ignore[no-untyped-def]
    assert embedder.dimension == 384
    [vector] = embedder.embed_passages(["A short passage."])
    assert len(vector) == 384


def test_model_captures_meaning_not_just_words(embedder) -> None:  # type: ignore[no-untyped-def]
    query = embedder.embed_query("How much money did the company make?")
    relevant, unrelated = embedder.embed_passages(
        [
            "Annual revenue rose to 4.2 billion dollars, driven by subscriptions.",
            "The hiking trail climbs steeply through pine forest to the summit.",
        ]
    )
    # The relevant passage shares no content words with the query.
    assert _cosine(query, relevant) > _cosine(query, unrelated) + 0.1
