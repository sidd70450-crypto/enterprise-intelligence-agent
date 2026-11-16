# Retrieval benchmark: retrieval-v1

Generated 2026-11-16T16:03:05+00:00 from commit `93823f213ad5` by `python -m evaluation.benchmarks.retrieval`. Every number below was measured in that run.

- Questions: 30 (paraphrase: 10, identifier: 10, factual: 10)
- Corpus: 10 documents
- Embedding model: `BAAI/bge-small-en-v1.5`; reranker: `Xenova/ms-marco-MiniLM-L-6-v2`

## Chunk size 400 characters (34 chunks indexed)

| Variant | Recall@1 | Recall@3 | Recall@5 | Recall@10 | MRR | p50 ms | p95 ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| vector | 0.93 | 1.00 | 1.00 | 1.00 | 0.967 | 9 | 13 |
| keyword | 0.77 | 0.83 | 0.83 | 0.87 | 0.794 | 2 | 4 |
| hybrid | 0.83 | 0.97 | 0.97 | 1.00 | 0.906 | 10 | 16 |
| hybrid + rerank | 0.90 | 1.00 | 1.00 | 1.00 | 0.950 | 208 | 228 |

Recall@3 by question type:

| Variant | factual | identifier | paraphrase |
| --- | --- | --- | --- |
| vector | 1.00 | 1.00 | 1.00 |
| keyword | 1.00 | 1.00 | 0.50 |
| hybrid | 1.00 | 1.00 | 0.90 |
| hybrid + rerank | 1.00 | 1.00 | 1.00 |

Questions missed at k=5: vector: none; keyword: q02, q03, q04, q08, q09; hybrid: q08; hybrid + rerank: none

## Chunk size 1500 characters (10 chunks indexed)

| Variant | Recall@1 | Recall@3 | Recall@5 | Recall@10 | MRR | p50 ms | p95 ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| vector | 0.93 | 1.00 | 1.00 | 1.00 | 0.967 | 7 | 16 |
| keyword | 0.77 | 0.93 | 0.93 | 0.93 | 0.833 | 2 | 2 |
| hybrid | 0.93 | 1.00 | 1.00 | 1.00 | 0.967 | 7 | 9 |
| hybrid + rerank | 1.00 | 1.00 | 1.00 | 1.00 | 1.000 | 148 | 160 |

Recall@3 by question type:

| Variant | factual | identifier | paraphrase |
| --- | --- | --- | --- |
| vector | 1.00 | 1.00 | 1.00 |
| keyword | 1.00 | 1.00 | 0.80 |
| hybrid | 1.00 | 1.00 | 1.00 |
| hybrid + rerank | 1.00 | 1.00 | 1.00 |

Questions missed at k=5: vector: none; keyword: q03, q04; hybrid: none; hybrid + rerank: none

## Limitations

- The dataset is small (30 questions over 10 short documents) and was written by the system's author, so results are indicative only and may not transfer to other corpora. With so few chunks, Recall@10 is close to the whole corpus; Recall@1, Recall@3 and MRR are the informative columns.
- Latency includes embedding the query on CPU and was measured on the machine that ran the benchmark, with the database on the same host.
- Each configuration ran once; no confidence intervals are reported for this run.
