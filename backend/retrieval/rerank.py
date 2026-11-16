"""Cross-encoder reranking.

A cross-encoder reads the query and one passage together and outputs a
relevance score. It is more accurate than comparing two separately computed
embeddings, and much slower, so it only rescores the top fused candidates.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from backend.config import Settings


class Reranker(Protocol):
    name: str

    def score(self, query: str, passages: Sequence[str]) -> list[float]: ...


class FastEmbedReranker:
    def __init__(self, model_name: str, cache_dir: Path | None = None) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self.name = model_name
        self._model: Any = TextCrossEncoder(
            model_name=model_name, cache_dir=str(cache_dir) if cache_dir else None
        )

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        return [float(s) for s in self._model.rerank(query, list(passages))]


def build_reranker(settings: Settings) -> Reranker | None:
    if settings.reranker_backend == "none":
        return None
    return FastEmbedReranker(settings.reranker_model, settings.embedding_cache_dir)
