import uuid

from backend.retrieval.search import RRF_K, reciprocal_rank_fusion

A, B, C, D = (uuid.uuid4() for _ in range(4))


def test_an_item_ranked_well_in_both_lists_wins() -> None:
    scores = reciprocal_rank_fusion([[A, B, C], [B, D, A]])
    assert max(scores, key=scores.__getitem__) == B  # ranks 2 and 1 beat A's 1 and 3


def test_scores_follow_the_formula() -> None:
    scores = reciprocal_rank_fusion([[A, B], [B]])
    assert scores[A] == 1 / (RRF_K + 1)
    assert scores[B] == 1 / (RRF_K + 2) + 1 / (RRF_K + 1)


def test_items_in_only_one_list_still_count() -> None:
    scores = reciprocal_rank_fusion([[A], [D]])
    assert set(scores) == {A, D}
    assert scores[A] == scores[D]


def test_empty_input() -> None:
    assert reciprocal_rank_fusion([]) == {}
    assert reciprocal_rank_fusion([[], []]) == {}
