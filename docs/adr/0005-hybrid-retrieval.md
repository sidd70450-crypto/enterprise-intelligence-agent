# ADR 0005: Hybrid retrieval and its benchmark

- Status: accepted
- Date: 2026-11-16

## Decision

Search runs up to four stages, all inside PostgreSQL except reranking:

1. **Vector search**: cosine distance on the chunk embeddings through the HNSW
   index, with `hnsw.ef_search` raised to 100 for the query so that the tenant
   filter does not starve the candidate list.
2. **Keyword search**: PostgreSQL full-text search through the GIN index. The
   query's terms are joined with OR (`plainto_tsquery` with `&` swapped for `|`),
   because natural-language questions rarely share every word with the passage
   that answers them. `ts_rank_cd` orders the matches. This is not BM25: it has
   no inverse document frequency, so common words weigh as much as rare ones. A
   BM25 extension can be added later behind the same function and benchmarked
   against this one.
3. **Reciprocal rank fusion** with k = 60 merges the two lists by rank alone.
4. **Reranking** with the `Xenova/ms-marco-MiniLM-L-6-v2` cross-encoder rescores
   the top 50 fused candidates. It can be turned off per request.

Every SQL statement filters by `org_id` and collection inside the query.
`POST /api/v1/search` returns 404 if any requested collection is not the caller's.

## The benchmark

`python -m evaluation.benchmarks.retrieval` indexes a dataset into a throwaway
organization, runs every question through the production `Retriever` in each mode,
and writes a report. Relevance is judged by evidence phrases, not chunk IDs, so
the labels survive changes to chunking. CI runs it with the real models on every
push; reports in `evaluation/reports/` are copied from those runs and never
edited by hand.

`retrieval-v1` is 30 questions over 10 short synthetic documents, written by the
system's author: 10 paraphrase questions sharing no keywords with their answer,
10 about exact identifiers, and 10 plain factual ones. It is a harness check and a
first baseline, not evidence of real-world quality.
