import itertools

import pytest

from backend.ingestion.chunking import chunk_pages
from backend.ingestion.parsing import Page

SENTENCE = "The quarterly revenue grew because enterprise customers renewed early. "


def _long_page(number: int | None = 1, sentences: int = 120) -> Page:
    return Page(number, (SENTENCE * sentences).strip())


def test_every_chunk_is_an_exact_slice_of_its_page() -> None:
    pages = [_long_page(1), _long_page(2, 40), Page(3, "Short final page.")]
    by_number = {p.number: p.text for p in pages}
    for chunk in chunk_pages(pages, target_chars=500, overlap_chars=80):
        assert by_number[chunk.page][chunk.char_start : chunk.char_end] == chunk.text


def test_chunks_respect_the_size_limit_and_never_cross_pages() -> None:
    chunks = chunk_pages([_long_page(1), _long_page(2)], target_chars=500, overlap_chars=80)
    assert all(len(c.text) <= 500 for c in chunks)
    assert {c.page for c in chunks} == {1, 2}
    pages_in_order = [c.page or 0 for c in chunks]
    assert pages_in_order == sorted(pages_in_order)


def test_chunks_end_on_sentence_boundaries_when_possible() -> None:
    chunks = chunk_pages([_long_page()], target_chars=500, overlap_chars=80)
    assert all(c.text.endswith(".") for c in chunks)


def test_consecutive_chunks_overlap_and_cover_the_whole_page() -> None:
    page = _long_page()
    chunks = chunk_pages([page], target_chars=500, overlap_chars=80)
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(page.text)
    for before, after in itertools.pairwise(chunks):
        assert after.char_start < before.char_end  # overlap, so no gap


def test_ordinals_are_sequential_across_pages() -> None:
    chunks = chunk_pages([_long_page(1), _long_page(2)], target_chars=500, overlap_chars=80)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_text_without_any_break_still_terminates() -> None:
    page = Page(1, "x" * 5000)
    chunks = chunk_pages([page], target_chars=1000, overlap_chars=100)
    assert chunks[-1].char_end == 5000
    assert all(len(c.text) <= 1000 for c in chunks)


def test_small_page_is_one_chunk_and_empty_page_is_none() -> None:
    chunks = chunk_pages([Page(1, "  Hello world.  "), Page(2, "")])
    assert [(c.page, c.text) for c in chunks] == [(1, "Hello world.")]


def test_overlap_must_be_under_half_the_target() -> None:
    with pytest.raises(ValueError):
        chunk_pages([_long_page()], target_chars=500, overlap_chars=250)
