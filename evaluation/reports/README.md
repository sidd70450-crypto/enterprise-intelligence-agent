# Benchmark reports

Each report here is copied unedited from a CI run of the benchmark. The link to the
run is the proof of where the numbers came from.

| Report | Produced by |
| --- | --- |
| [retrieval-v1, bge-small + MiniLM reranker](retrieval-retrieval-v1-BAAI_bge-small-en-v1.5.md) | [CI run 43903031254](https://github.com/sidd70450-crypto/enterprise-intelligence-agent/actions/runs/43903031254), job `retrieval-benchmark`, PR #3 |

The commit shown inside a report is the commit GitHub Actions checked out. For a pull
request that is the temporary merge of the branch into `main`.

## Reading the retrieval-v1 results

These readings are the author's interpretation of the measured numbers, not further
measurements.

- **The dataset is too easy to separate the methods.** Vector search alone reaches
  Recall@3 of 1.00, so most columns hit the ceiling. A larger corpus with harder
  distractors is the main need (milestone M11).
- **Fusion can hurt the top result.** With 400-character chunks, hybrid search has a
  lower Recall@1 than vector search alone (0.83 against 0.93): reciprocal rank fusion
  gives the weaker keyword ranking equal weight. Weighted fusion is worth testing.
- **Reranking restores the top result but costs about 20 times the latency**
  (roughly 150 to 230 ms per query on a CI runner's CPU, against under 20 ms
  without). Whether that is worth it depends on the research workflow's latency
  budget, decided in M4 and M5.
- **Keyword search is weakest on paraphrase questions** (Recall@3 0.50 at 400
  characters), as expected from a method that matches words, not meaning.
