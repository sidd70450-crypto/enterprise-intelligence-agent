import json
from pathlib import Path

import pytest

from evaluation.metrics.retrieval import aggregate, normalize, percentile, score_question

DATASET = Path(__file__).resolve().parents[2] / "evaluation/datasets/retrieval-v1"


def test_relevance_ignores_case_and_whitespace() -> None:
    scores = score_question(["Revenue  ROSE\nto 4 billion"], ["revenue rose to"], ks=[1])
    assert scores.recall[1] == 1.0 and scores.reciprocal_rank == 1.0


def test_reciprocal_rank_uses_the_first_relevant_hit() -> None:
    scores = score_question(["a", "b", "the answer", "the answer again"], ["answer"], ks=[3])
    assert scores.reciprocal_rank == pytest.approx(1 / 3)
    assert scores.precision[3] == pytest.approx(1 / 3)


def test_recall_counts_each_evidence_string_once() -> None:
    scores = score_question(["alpha here", "beta here"], ["alpha", "beta", "gamma"], ks=[1, 2])
    assert scores.recall == {1: pytest.approx(1 / 3), 2: pytest.approx(2 / 3)}


def test_nothing_relevant_scores_zero() -> None:
    scores = score_question(["x", "y"], ["z"], ks=[2])
    assert (scores.recall[2], scores.reciprocal_rank) == (0.0, 0.0)


def test_aggregate_averages_questions() -> None:
    a = score_question(["hit"], ["hit"], ks=[1])
    b = score_question(["miss"], ["hit"], ks=[1])
    assert aggregate([a, b], ks=[1]) == {"recall@1": 0.5, "precision@1": 0.5, "mrr": 0.5}


def test_percentile_nearest_rank() -> None:
    assert percentile([5, 1, 3, 2, 4], 50) == 3
    assert percentile([5, 1, 3, 2, 4], 95) == 5
    assert percentile([], 50) == 0.0


def test_every_evidence_string_exists_in_the_corpus() -> None:
    """Guards the dataset itself: a label pointing at missing text scores as a miss forever."""
    corpus = [normalize(p.read_text()) for p in (DATASET / "corpus").glob("*.md")]
    for line in (DATASET / "questions.jsonl").read_text().splitlines():
        question = json.loads(line)
        for evidence in question["evidence"]:
            assert any(normalize(evidence) in doc for doc in corpus), (question["id"], evidence)
