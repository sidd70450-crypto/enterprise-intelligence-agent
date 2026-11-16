"""Turning text into vectors.

``Embedder`` is the interface the rest of the system uses. Two implementations:

- ``FastEmbedEmbedder`` runs BAAI/bge-small-en-v1.5 locally through ONNX
  Runtime. No per-token cost, no data sent to a third party. The model weights
  (about 70 MB) are downloaded from Hugging Face on first use and cached.
- ``HashingEmbedder`` hashes words into a fixed-size vector. It is
  deterministic, needs no download and runs in microseconds, which makes it right
  for tests. It matches shared words only and understands nothing: never report
  retrieval quality measured with it as the system's quality.
"""

import hashlib
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from backend.config import Settings
from backend.database.documents import EMBEDDING_DIMENSION

_WORD = re.compile(r"\w+", re.UNICODE)


class Embedder(Protocol):
    name: str
    dimension: int

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashingEmbedder:
    name = "hashing-v1"

    def __init__(self, dimension: int = EMBEDDING_DIMENSION) -> None:
        self.dimension = dimension

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for word in _WORD.findall(text.lower()):
            digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimension
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class FastEmbedEmbedder:
    def __init__(self, model_name: str, cache_dir: Path | None = None) -> None:
        from fastembed import TextEmbedding

        self.name = model_name
        self._model: Any = TextEmbedding(
            model_name=model_name, cache_dir=str(cache_dir) if cache_dir else None
        )
        probe = next(iter(self._model.embed(["dimension probe"])))
        self.dimension = len(probe)

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.passage_embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        vector = next(iter(self._model.query_embed(text)))
        return list(vector.tolist())


def build_embedder(settings: Settings) -> Embedder:
    embedder: Embedder
    if settings.embedding_backend == "hashing":
        embedder = HashingEmbedder()
    else:
        embedder = FastEmbedEmbedder(settings.embedding_model, settings.embedding_cache_dir)
    if embedder.dimension != EMBEDDING_DIMENSION:
        raise RuntimeError(
            f"Embedding model {embedder.name} produces {embedder.dimension}-dimensional "
            f"vectors; the database column holds {EMBEDDING_DIMENSION}."
        )
    return embedder
