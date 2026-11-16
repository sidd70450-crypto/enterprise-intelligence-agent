"""Split page text into overlapping passages for retrieval.

Chunks never cross a page boundary, so every chunk has one page number to cite.
Inside a page, a chunk ends at the best natural break before the size limit:
a blank line, then a sentence end, then a space. Consecutive chunks overlap so a
sentence cut at a boundary still appears whole in one of them.

Each chunk records ``char_start`` and ``char_end`` such that
``page.text[char_start:char_end] == chunk.text``. The verification engine (M5)
relies on this to check that a quote really comes from the cited page.

Sizes are in characters, not model tokens. English text averages about four
characters per token, so the default 1,500 characters is about 375 tokens,
inside the embedding model's 512-token limit.
"""

from dataclasses import dataclass

from backend.ingestion.parsing import Page

DEFAULT_TARGET_CHARS = 1500
DEFAULT_OVERLAP_CHARS = 200
_BREAKS = ("\n\n", ". ", "? ", "! ", "\n", " ")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    page: int | None
    char_start: int
    char_end: int
    text: str


def _cut_point(text: str, start: int, limit: int) -> int:
    """Where to end a chunk that starts at ``start`` and may not pass ``limit``."""
    earliest = start + (limit - start) // 2  # never cut in the first half
    for marker in _BREAKS:
        at = text.rfind(marker, earliest, limit)
        if at != -1:
            return at + len(marker.rstrip(" ")) if marker != " " else at
    return limit  # no break at all: a very long word or URL


def _strip(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def chunk_pages(
    pages: list[Page],
    target_chars: int = DEFAULT_TARGET_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    if not 0 <= overlap_chars < target_chars // 2:
        raise ValueError("overlap_chars must be at least 0 and under half of target_chars")

    chunks: list[Chunk] = []
    for page in pages:
        text, start = page.text, 0
        while start < len(text):
            limit = min(start + target_chars, len(text))
            end = limit if limit == len(text) else _cut_point(text, start, limit)
            s, e = _strip(text, start, end)
            if s < e:
                chunks.append(Chunk(len(chunks), page.number, s, e, text[s:e]))
            if end >= len(text):
                break
            # Step back by the overlap, then forward to the next word so the
            # next chunk does not begin mid-word. Always move forward overall.
            next_start = max(end - overlap_chars, start + 1)
            space = text.find(" ", next_start, end)
            start = space + 1 if space != -1 else next_start
    return chunks
