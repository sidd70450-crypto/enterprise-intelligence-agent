"""Retrieval metrics.

A question lists one or more *evidence* strings: short exact phrases from the
corpus that answer it. A retrieved chunk is relevant if it contains one of them
(case and whitespace ignored). Judging by evidence text rather than chunk IDs
keeps the labels valid when the chunking changes.

- recall@k: share of a question's evidence strings found in its top k chunks.
- precision@k: share of the top k chunks that contain any evidence string.
- reciprocal rank: 1 / rank of the first relevant chunk, 0 if none in the list.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import mean


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass(frozen=True)
class QuestionScores:
    recall: dict[int, float]
    precision: dict[int, float]
    reciprocal_rank: float


def score_question(
    retrieved: Sequence[str], evidence: Sequence[str], ks: Sequence[int]
) -> QuestionScores:
    texts = [normalize(t) for t in retrieved]
    wanted = [normalize(e) for e in evidence]
    relevant = [any(e in t for e in wanted) for t in texts]
    recall = {k: sum(any(e in t for t in texts[:k]) for e in wanted) / len(wanted) for k in ks}
    precision = {k: sum(relevant[:k]) / k for k in ks}
    first = next((rank for rank, hit in enumerate(relevant, start=1) if hit), None)
    return QuestionScores(recall, precision, 1 / first if first else 0.0)


def aggregate(scores: Sequence[QuestionScores], ks: Sequence[int]) -> dict[str, float]:
    if not scores:
        return {}
    result = {f"recall@{k}": mean(s.recall[k] for s in scores) for k in ks}
    result |= {f"precision@{k}": mean(s.precision[k] for s in scores) for k in ks}
    result["mrr"] = mean(s.reciprocal_rank for s in scores)
    return result


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile; ``q`` in [0, 100]."""
    if not values:
        return 0.0
    ordered = sorted(values)
    # Nearest rank is the ceiling. round() would be wrong: it rounds 2.5 to 2.
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]
