"""Checking that a quote really appears in its source.

Models copy text imperfectly: they collapse line breaks, add or drop spaces.
``locate_quote`` accepts a quote whose words appear in the source in the same
order with only whitespace between them, and returns the exact span of the
source text it matched. Any other difference (a changed word, a paraphrase, an
invented sentence) is a miss, and the quote is discarded.

This check is done in code, never by a model: whether a string is in a passage
is a fact, not a judgment.
"""

import re

MIN_QUOTE_CHARS = 8


def locate_quote(source: str, quote: str) -> tuple[int, int] | None:
    """The (start, end) of ``quote`` in ``source``, ignoring whitespace differences."""
    words = quote.split()
    if len("".join(words)) < MIN_QUOTE_CHARS:
        return None  # too short to be meaningful evidence
    start = source.find(quote)
    if start != -1:
        return start, start + len(quote)
    pattern = r"\s+".join(re.escape(word) for word in words)
    match = re.search(pattern, source)
    return (match.start(), match.end()) if match else None
